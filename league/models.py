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

class RosterSpot(models.Model):
    ACQUIRED_CHOICES = [
        ("DRAFT", "Draft"),
        ("FA", "Free Agency"),
        ("TRADE", "Trade"),
    ]

    draft = models.ForeignKey("Draft", on_delete=models.CASCADE, related_name="roster_spots")
    team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="roster_spots")
    player = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="roster_spots")

    acquired_via = models.CharField(max_length=10, choices=ACQUIRED_CHOICES, default="DRAFT")
    acquired_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            # impede o mesmo jogador estar em 2 times no MESMO draft
            models.UniqueConstraint(fields=["draft", "player"], name="uniq_player_per_draft_roster"),
        ]

    def __str__(self):
        return f"{self.team.name}: {self.player.name} ({self.acquired_via})"

