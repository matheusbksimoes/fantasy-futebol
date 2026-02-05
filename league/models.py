from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


# -----------------------------
# Core
# -----------------------------
class League(models.Model):
    name = models.CharField(max_length=100)
    season = models.IntegerField()

    def __str__(self):
        return f"{self.name} ({self.season})"


class Team(models.Model):
    league = models.ForeignKey("League", on_delete=models.CASCADE, related_name="teams")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teams",
    )
    name = models.CharField(max_length=100)
    budget = models.IntegerField(default=100)

    def __str__(self):
        return self.name


class Player(models.Model):
    POSITION_CHOICES = [
        ("GOL", "Goleiro"),
        ("LAT", "Lateral"),
        ("ZAG", "Zagueiro"),
        ("MEI", "Meia"),
        ("ATA", "Atacante"),
        ("TEC", "Técnico"),
    ]

    # Cartola ID único quando existe (NULL pode repetir no Postgres; isso é OK).
    cartola_id = models.IntegerField(unique=True, null=True, blank=True)

    name = models.CharField(max_length=100)
    position = models.CharField(max_length=3, choices=POSITION_CHOICES)
    real_team = models.CharField(max_length=100)

    def __str__(self):
        return self.name


# -----------------------------
# Draft
# -----------------------------
class Draft(models.Model):
    DRAFT_TYPE_CHOICES = [
        ("snake", "Snake"),
        ("linear", "Linear"),
    ]

    league = models.OneToOneField("League", on_delete=models.CASCADE, related_name="draft")
    draft_type = models.CharField(max_length=10, choices=DRAFT_TYPE_CHOICES, default="snake")
    rounds = models.IntegerField(default=20)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Draft {self.league} - {self.rounds} rounds ({self.draft_type})"


class DraftPick(models.Model):
    draft = models.ForeignKey("Draft", on_delete=models.CASCADE, related_name="picks")
    round_number = models.IntegerField()
    pick_number = models.IntegerField()
    overall_number = models.IntegerField()
    team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="draft_picks")
    player = models.ForeignKey("Player", on_delete=models.SET_NULL, null=True, blank=True)
    made_at = models.DateTimeField(null=True, blank=True)

    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["overall_number"]
        constraints = [
            models.UniqueConstraint(fields=["draft", "overall_number"], name="uniq_overall_per_draft"),
            models.UniqueConstraint(
                fields=["draft", "round_number", "pick_number"],
                name="uniq_round_pick_per_draft",
            ),
            models.UniqueConstraint(fields=["draft", "player"], name="uniq_player_per_draft_pick"),
        ]

    def __str__(self):
        player_name = self.player.name if self.player else "—"
        return (
            f"R{self.round_number} P{self.pick_number} "
            f"(#{self.overall_number}) - {self.team.name} -> {player_name}"
        )

    def clean(self):
        if self.player_id:
            exists = (
                DraftPick.objects.filter(draft=self.draft, player_id=self.player_id)
                .exclude(id=self.id)
                .exists()
            )
            if exists:
                raise ValidationError("Este jogador já foi draftado neste draft.")

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        previous_player_id = None

        if not is_new:
            previous_player_id = DraftPick.objects.only("player_id").get(pk=self.pk).player_id

        super().save(*args, **kwargs)

        # Se o player foi definido agora (e antes não tinha / mudou)
        if self.player_id and self.player_id != previous_player_id:
            RosterSpot.objects.get_or_create(
                draft=self.draft,
                player=self.player,
                defaults={
                    "team": self.team,
                    "acquired_via": "DRAFT",
                    "acquired_at": timezone.now(),
                    "dropped_at": None,
                },
            )


# -----------------------------
# Week / Schedule
# -----------------------------
class Week(models.Model):
    draft = models.ForeignKey("Draft", on_delete=models.CASCADE, related_name="weeks")
    number = models.PositiveIntegerField()  # 1, 2, 3...

    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    is_current = models.BooleanField(default=False)

    is_playoff = models.BooleanField(default=False)
    is_final = models.BooleanField(default=False)

    # Week 1 manual travada, etc.
    is_locked = models.BooleanField(default=False)
    is_postponed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["draft", "number"], name="uniq_week_per_draft"),
        ]
        ordering = ["draft_id", "number"]

    def __str__(self):
        label = f"Draft {self.draft_id} — Week {self.number}"

        if self.is_final:
            label += " (Final)"
        elif self.is_playoff:
            label += " (Playoffs)"

        if self.is_postponed:
            label += " [ADIADA]"

        return label


class Matchup(models.Model):
    week = models.ForeignKey("Week", on_delete=models.CASCADE, related_name="matchups")

    home_team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="home_matchups")
    away_team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="away_matchups")

    home_score = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    away_score = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    is_final = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["week", "home_team", "away_team"], name="uniq_matchup_per_week"),
        ]

    def __str__(self):
        return f"Week {self.week.number}: {self.home_team.name} x {self.away_team.name}"


# -----------------------------
# Roster / Transactions
# -----------------------------
class RosterSpot(models.Model):
    ACQUIRED_CHOICES = [
        ("DRAFT", "Draft"),
        ("WAIVER", "Waiver"),
        ("FA", "Free Agency"),
        ("TRADE", "Trade"),
    ]

    draft = models.ForeignKey("Draft", on_delete=models.CASCADE, related_name="roster_spots")
    team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="roster_spots")
    player = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="roster_spots")

    acquired_via = models.CharField(max_length=10, choices=ACQUIRED_CHOICES, default="DRAFT")
    acquired_at = models.DateTimeField(default=timezone.now)

    dropped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # 1 player ativo globalmente (não pode estar em 2 times)
            models.UniqueConstraint(
                fields=["player"],
                condition=Q(dropped_at__isnull=True),
                name="uniq_active_player_global",
            ),
            # histórico: não duplica player dentro do mesmo draft
            models.UniqueConstraint(fields=["draft", "player"], name="uniq_player_per_draft_roster"),
        ]

    def __str__(self):
        status = "ACTIVE" if self.dropped_at is None else "DROPPED"
        return f"{self.team.name}: {self.player.name} ({self.acquired_via}) - {status}"


class Transaction(models.Model):
    TYPE_CHOICES = [
        ("DRAFT", "Draft"),
        ("ADD", "Add (FA)"),
        ("DROP", "Drop"),
        ("TRADE", "Trade"),
    ]

    draft = models.ForeignKey("Draft", on_delete=models.CASCADE, related_name="transactions")
    team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="transactions")
    player = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="transactions")

    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.created_at:%d/%m %H:%M} - {self.team} - {self.type} - {self.player}"


# -----------------------------
# Lineup (por Week) + Formation (anti-fraude)
# -----------------------------
FORMATIONS = [
    ("343", "3-4-3"),
    ("352", "3-5-2"),
    ("442", "4-4-2"),
    ("433", "4-3-3"),
    ("532", "5-3-2"),
    ("541", "5-4-1"),
]

# Quantidade por posição de linha (GOL e TEC são sempre 1)
FORMATION_MAP = {
    "343": {"ZAG": 3, "LAT": 0, "MEI": 4, "ATA": 3},
    "352": {"ZAG": 3, "LAT": 0, "MEI": 5, "ATA": 2},
    "442": {"ZAG": 2, "LAT": 2, "MEI": 4, "ATA": 2},
    "433": {"ZAG": 2, "LAT": 2, "MEI": 3, "ATA": 3},
    "532": {"ZAG": 3, "LAT": 2, "MEI": 3, "ATA": 2},
    "541": {"ZAG": 3, "LAT": 2, "MEI": 4, "ATA": 1},
}


class Lineup(models.Model):
    """
    Um Lineup por (week, team) com formação anti-fraude.
    Spots ficam em LineupSpot (ZAG1.., LAT1.., MEI1.., ATA1.., + GOL1 e TEC1).
    """
    week = models.ForeignKey("Week", on_delete=models.CASCADE, related_name="lineups")
    team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="lineups")
    formation = models.CharField(max_length=3, choices=FORMATIONS, default="433")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["week", "team"], name="uniq_lineup_week_team"),
        ]

    def clean(self):
        if self.formation not in FORMATION_MAP:
            raise ValidationError("Formação inválida.")

    def __str__(self):
        return f"Week {self.week.number} — {self.team.name} ({self.formation})"


class LineupSpot(models.Model):
    """
    Slot com índice:
      - GOL1
      - TEC1
      - ZAG1..ZAG5
      - LAT1..LAT2
      - MEI1..MEI5
      - ATA1..ATA3
    """

    SLOT_TYPE_CHOICES = [
        ("GOL", "Goleiro"),
        ("ZAG", "Zagueiro"),
        ("LAT", "Lateral"),
        ("MEI", "Meia"),
        ("ATA", "Atacante"),
        ("TEC", "Técnico"),
    ]

    # ✅ Nesta fase, deixamos NULL permitido para NÃO quebrar banco legado.
    # A view nova sempre cria LineupSpot já com lineup+slot_type+slot_index, e depois atribui player.
    lineup = models.ForeignKey(
        "Lineup",
        on_delete=models.CASCADE,
        related_name="spots",
        null=True,
        blank=True,
    )

    slot_type = models.CharField(
        max_length=3,
        choices=SLOT_TYPE_CHOICES,
        null=True,
        blank=True,
    )

    slot_index = models.PositiveIntegerField(
        null=True,
        blank=True,
    )

    # ✅ PRECISA ser NULL para podermos criar os slots da formação primeiro
    player = models.ForeignKey(
        "Player",
        on_delete=models.CASCADE,
        related_name="lineup_spots",
        null=True,
        blank=True,
    )

    set_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # ✅ só aplica unicidade quando lineup/slot_type/slot_index estiverem preenchidos
            models.UniqueConstraint(
                fields=["lineup", "slot_type", "slot_index"],
                condition=Q(lineup__isnull=False, slot_type__isnull=False, slot_index__isnull=False),
                name="uniq_lineup_slot_filled",
            ),
            # ✅ só aplica unicidade quando player existir (evita “bloquear” slots vazios)
            models.UniqueConstraint(
                fields=["lineup", "player"],
                condition=Q(lineup__isnull=False, player__isnull=False),
                name="uniq_player_in_lineup_filled",
            ),
        ]

    def clean(self):
        # enquanto estiver em transição, não valida registros incompletos
        if not self.lineup_id or not self.player_id or not self.slot_type or not self.slot_index:
            return

        if self.player.position != self.slot_type:
            raise ValidationError(
                f"{self.player.name} é {self.player.position} e não pode ser escalado em {self.slot_type}{self.slot_index}."
            )

    def __str__(self):
        lineup_label = str(self.lineup) if self.lineup_id else "Lineup(NULL)"
        player_label = self.player.name if self.player_id else "Player(NULL)"
        slot_label = f"{self.slot_type}{self.slot_index}" if self.slot_type and self.slot_index else "Slot(NULL)"
        return f"{lineup_label} — {slot_label}: {player_label}"



# -----------------------------
# Scores
# -----------------------------
class PlayerWeekScore(models.Model):
    SOURCE_CHOICES = [
        ("CARTOLA", "Cartola"),
        ("MANUAL", "Manual"),
    ]

    week = models.ForeignKey("Week", on_delete=models.CASCADE, related_name="player_scores")
    player = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="week_scores")

    points = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    scouts = models.JSONField(default=dict, blank=True)

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="CARTOLA")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["week", "player"], name="uniq_player_score_per_week"),
        ]

    def __str__(self):
        return f"Week {self.week.number} — {self.player.name}: {self.points}"
# league/models.py
from django.db import models
from django.core.validators import MinValueValidator

# -----------------------------
# FAAB / Waivers
# -----------------------------
from django.core.validators import MinValueValidator

class TeamBudget(models.Model):
    # ⚠️ NÃO pode ser related_name="budget" porque Team já tem um field chamado budget
    team = models.OneToOneField(
        "league.Team",
        on_delete=models.CASCADE,
        related_name="faab_budget",  # <-- único ajuste necessário
    )
    faab_balance = models.PositiveIntegerField(default=100)

    def __str__(self):
        return f"{self.team} - FAAB: {self.faab_balance}"


class WaiverClaim(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        WON = "WON", "Won"
        LOST = "LOST", "Lost"
        INVALID = "INVALID", "Invalid"

    team = models.ForeignKey("league.Team", on_delete=models.CASCADE, related_name="waiver_claims")
    add_player = models.ForeignKey("league.Player", on_delete=models.CASCADE, related_name="waiver_add_claims")
    drop_player = models.ForeignKey(
        "league.Player",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="waiver_drop_claims",
    )

    bid = models.PositiveIntegerField(validators=[MinValueValidator(0)], default=0)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    invalid_reason = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "add_player", "-bid", "created_at"]),
        ]

    def __str__(self):
        return f"{self.team} -> ADD {self.add_player} (${self.bid}) [{self.status}]"
