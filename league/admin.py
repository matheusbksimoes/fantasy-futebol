from django.contrib import admin

from .models import (
    League,
    Team,
    Player,
    Draft,
    DraftPick,
    Week,
    Matchup,
    RosterSpot,
    Transaction,
    Lineup,
    LineupSpot,
    PlayerWeekScore,
)


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "season")
    search_fields = ("name",)
    ordering = ("-season", "name")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "league", "user", "budget")
    list_filter = ("league",)
    search_fields = ("name",)
    ordering = ("league", "name")


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "position", "real_team", "cartola_id")
    list_filter = ("position", "real_team")
    search_fields = ("name", "real_team")
    ordering = ("position", "name")


@admin.register(Draft)
class DraftAdmin(admin.ModelAdmin):
    list_display = ("id", "league", "draft_type", "rounds", "created_at")
    list_filter = ("draft_type",)
    ordering = ("-id",)


@admin.register(DraftPick)
class DraftPickAdmin(admin.ModelAdmin):
    list_display = ("id", "draft", "overall_number", "round_number", "pick_number", "team", "player", "is_current")
    list_filter = ("draft", "round_number", "team", "is_current")
    search_fields = ("team__name", "player__name")
    ordering = ("draft", "overall_number")


@admin.register(Week)
class WeekAdmin(admin.ModelAdmin):
    list_display = ("id", "draft", "number", "is_current", "is_locked", "is_postponed", "is_playoff", "is_final")
    list_filter = ("draft", "is_current", "is_locked", "is_postponed", "is_playoff", "is_final")
    ordering = ("draft", "number")


@admin.register(Matchup)
class MatchupAdmin(admin.ModelAdmin):
    list_display = ("id", "week", "home_team", "away_team", "home_score", "away_score", "is_final")
    list_filter = ("week", "is_final")
    ordering = ("week", "id")


@admin.register(RosterSpot)
class RosterSpotAdmin(admin.ModelAdmin):
    list_display = ("id", "draft", "team", "player", "acquired_via", "acquired_at", "dropped_at")
    list_filter = ("draft", "acquired_via")
    search_fields = ("team__name", "player__name")
    ordering = ("draft", "team", "player")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "draft", "team", "player", "type", "created_at")
    list_filter = ("draft", "type")
    search_fields = ("team__name", "player__name")
    ordering = ("-created_at",)


# ✅ Lineup (week+team) com formation
@admin.register(Lineup)
class LineupAdmin(admin.ModelAdmin):
    list_display = ("id", "week", "team", "formation", "created_at", "updated_at")
    list_filter = ("week", "formation")
    search_fields = ("team__name",)
    ordering = ("-week__number", "team__name")


# ✅ LineupSpot depende de Lineup
@admin.register(LineupSpot)
class LineupSpotAdmin(admin.ModelAdmin):
    # ✅ seguro mesmo com lineup NULL (fase de migração)
    list_display = ("id", "get_week", "get_team", "slot_type", "slot_index", "player", "set_at")

    # ✅ filtros via relacionamento (e um filtro extra pra achar linhas “órfãs”)
    list_filter = ("slot_type", "lineup__week", "lineup__team")

    search_fields = ("player__name", "lineup__team__name")
    ordering = ("lineup__week__number", "lineup__team__name", "slot_type", "slot_index")

    @admin.display(description="Week", ordering="lineup__week__number")
    def get_week(self, obj):
        return obj.lineup.week if obj.lineup_id else "—"

    @admin.display(description="Team", ordering="lineup__team__name")
    def get_team(self, obj):
        return obj.lineup.team if obj.lineup_id else "—"


@admin.register(PlayerWeekScore)
class PlayerWeekScoreAdmin(admin.ModelAdmin):
    list_display = ("id", "week", "player", "points", "source", "fetched_at")
    list_filter = ("week", "source")
    search_fields = ("player__name",)
    ordering = ("-week__number", "-points")
