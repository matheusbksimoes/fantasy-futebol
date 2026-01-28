from django.db import models
from django.conf import settings


class League(models.Model):
    name = models.CharField(max_length=100)
    season = models.IntegerField()

    def __str__(self):
        return f"{self.name} ({self.season})"


class Team(models.Model):
    league = models.ForeignKey("League", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    name = models.CharField(max_length=100)
    budget = models.IntegerField(default=100)

    def __str__(self):
        return self.name



class Player(models.Model):
    POSITION_CHOICES = [
        ('GOL', 'Goleiro'),
        ('LAT', 'Lateral'),
        ('ZAG', 'Zagueiro'),
        ('MEI', 'Meia'),
        ('ATA', 'Atacante'),
        ('TEC', 'Técnico'),
    ]

    cartola_id = models.IntegerField(unique=True, null=True, blank=True)  # <-- ADICIONE
    name = models.CharField(max_length=100)
    position = models.CharField(max_length=3, choices=POSITION_CHOICES)
    real_team = models.CharField(max_length=100)

    def __str__(self):
        return self.name




class Roster(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    player = models.ForeignKey(Player, on_delete=models.CASCADE)
    active = models.BooleanField(default=True)
    def __str__(self):
        return f"{self.team.name} - {self.player.name}"


class Matchup(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE)
    week = models.IntegerField()
    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='home_games')
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='away_games')


class Score(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE)
    week = models.IntegerField()
    points = models.FloatField()
    source = models.CharField(
        max_length=20,
        choices=[('cartola', 'Cartola'), ('manual', 'Manual')]
    )
class Draft(models.Model):
    DRAFT_TYPE_CHOICES = [
        ('snake', 'Snake'),
        ('linear', 'Linear'),
    ]

    league = models.OneToOneField(League, on_delete=models.CASCADE, related_name='draft')
    draft_type = models.CharField(max_length=10, choices=DRAFT_TYPE_CHOICES, default='snake')
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
        unique_together = [
            ("draft", "overall_number"),
            ("draft", "round_number", "pick_number"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["draft", "player"], name="unique_player_per_draft"),
        ]

    def __str__(self):
        player_name = self.player.name if self.player else "—"
        return (
            f"R{self.round_number} P{self.pick_number} "
            f"(#{self.overall_number}) - {self.team.name} -> {player_name}"
        )
    def clean(self):
        if self.player:
            exists = DraftPick.objects.filter(
                draft=self.draft,
                player=self.player
            ).exclude(id=self.id).exists()

            if exists:
                from django.core.exceptions import ValidationError
                raise ValidationError("Este jogador já foi draftado neste draft.")
    def save(self, *args, **kwargs):
        is_new_pick = self.pk is None
        previous_player = None

        if not is_new_pick:
            previous_player = DraftPick.objects.get(pk=self.pk).player

        super().save(*args, **kwargs)

        # Se o player foi definido agora (e antes não tinha)
        if self.player and previous_player != self.player:
            from league.models import Roster
            Roster.objects.get_or_create(
                team=self.team,
                player=self.player,
                defaults={"active": True}
            )
from django.utils import timezone

from django.db import models
from django.db.models import Q
from django.utils import timezone


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

    # ✅ NOVO: marca que o jogador foi dropado
    dropped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # ✅ NOVO: impede jogador estar ativo em 2 times ao mesmo tempo
            models.UniqueConstraint(
                fields=["player"],
                condition=Q(dropped_at__isnull=True),
                name="uniq_active_player_global",
            ),
            # mantém sua regra original
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

    draft = models.ForeignKey("Draft", on_delete=models.CASCADE)
    team = models.ForeignKey("Team", on_delete=models.CASCADE)
    player = models.ForeignKey("Player", on_delete=models.CASCADE)

    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.created_at:%d/%m %H:%M} - {self.team} - {self.type} - {self.player}"

class Week(models.Model):
    draft = models.ForeignKey(
        "Draft",
        on_delete=models.CASCADE,
        related_name="weeks"
    )

    number = models.PositiveIntegerField()  # 1, 2, 3...

    # Datas reais (opcional, mas útil)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    # Controle de estado
    is_current = models.BooleanField(default=False)

    # 🔹 NOVOS CAMPOS (C4.8)
    is_playoff = models.BooleanField(default=False)
    is_final = models.BooleanField(default=False)

    is_locked = models.BooleanField(default=False)
    is_postponed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["draft", "number"],
                name="uniq_week_per_draft"
            ),
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

class LineupSpot(models.Model):
    SLOT_CHOICES = [
        ("GOL", "Goleiro"),
        ("LAT", "Lateral"),
        ("ZAG", "Zagueiro"),
        ("MEI", "Meia"),
        ("ATA", "Atacante"),
        ("TEC", "Técnico"),
        ("BENCH", "Banco"),
    ]

    week = models.ForeignKey("Week", on_delete=models.CASCADE, related_name="lineup_spots")
    team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="lineup_spots")
    player = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="lineup_spots")

    slot = models.CharField(max_length=10, choices=SLOT_CHOICES)
    set_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # jogador não pode estar em 2 times na mesma semana
            models.UniqueConstraint(fields=["week", "player"], name="uniq_player_per_week"),
            # jogador não pode ser escalado 2x no mesmo time na mesma semana (redundante mas ajuda)
            models.UniqueConstraint(fields=["week", "team", "player"], name="uniq_player_team_week"),
        ]

    def __str__(self):
        return f"Week {self.week.number} — {self.team.name}: {self.player.name} ({self.slot})"

from django.db import models
from django.utils import timezone

class PlayerWeekScore(models.Model):
    week = models.ForeignKey("Week", on_delete=models.CASCADE, related_name="player_scores")
    player = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="week_scores")

    points = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    scouts = models.JSONField(default=dict, blank=True)

    source = models.CharField(max_length=20, default="CARTOLA")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["week", "player"], name="uniq_player_score_per_week"),
        ]

    def __str__(self):
        return f"Week {self.week.number} — {self.player.name}: {self.points}"

class Matchup(models.Model):
    week = models.ForeignKey("Week", on_delete=models.CASCADE, related_name="matchups")

    home_team = models.ForeignKey(
        "Team",
        on_delete=models.CASCADE,
        related_name="home_matchups",
    )
    away_team = models.ForeignKey(
        "Team",
        on_delete=models.CASCADE,
        related_name="away_matchups",
    )

    home_score = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    away_score = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    is_final = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["week", "home_team", "away_team"],
                name="uniq_matchup_per_week",
            ),
        ]

    def __str__(self):
        return f"Week {self.week.number}: {self.home_team.name} x {self.away_team.name}"

# league/models.py
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import UniqueConstraint

FORMATIONS = [
    ("343", "3-4-3"),
    ("352", "3-5-2"),
    ("451", "4-5-1"),
    ("442", "4-4-2"),
    ("433", "4-3-3"),
    ("532", "5-3-2"),
    ("541", "5-4-1"),
]

SLOT_TYPES = [
    ("GOL", "Goleiro"),
    ("ZAG", "Zagueiro"),
    ("LAT", "Lateral"),
    ("MEI", "Meia"),
    ("ATA", "Atacante"),
    ("TEC", "Técnico"),
]

FORMATION_MAP = {
    "343": {"ZAG": 3, "LAT": 0, "MEI": 4, "ATA": 3},
    "352": {"ZAG": 3, "LAT": 0, "MEI": 5, "ATA": 2},
    "451": {"ZAG": 2, "LAT": 2, "MEI": 5, "ATA": 1},
    "442": {"ZAG": 2, "LAT": 2, "MEI": 4, "ATA": 2},
    "433": {"ZAG": 2, "LAT": 2, "MEI": 3, "ATA": 3},
    "532": {"ZAG": 3, "LAT": 2, "MEI": 3, "ATA": 2},
    "541": {"ZAG": 3, "LAT": 2, "MEI": 4, "ATA": 1},
}

class Lineup(models.Model):
    fantasy_team = models.ForeignKey("league.Team", on_delete=models.CASCADE)
    round_number = models.PositiveIntegerField()
    formation = models.CharField(max_length=3, choices=FORMATIONS, default="433")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["fantasy_team", "round_number"], name="uniq_lineup_team_round")
        ]

    def clean(self):
        if self.formation not in FORMATION_MAP:
            raise ValidationError("Formação inválida.")

class LineupSlot(models.Model):
    lineup = models.ForeignKey(Lineup, on_delete=models.CASCADE, related_name="slots")
    slot_type = models.CharField(max_length=3, choices=SLOT_TYPES)
    slot_index = models.PositiveIntegerField()  # 1..N dentro do tipo (DEF1, DEF2...)
    player = models.ForeignKey("league.Player", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["lineup", "slot_type", "slot_index"], name="uniq_lineup_slot")
        ]

    def __str__(self):
        return f"{self.lineup.fantasy_team} R{self.lineup.round_number} {self.slot_type}{self.slot_index}"

