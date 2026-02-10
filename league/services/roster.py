# league/services/roster.py
from __future__ import annotations

from django.utils import timezone
from django.db import transaction
from django.core.exceptions import ValidationError

from league.models import Draft, Player, Team, RosterSpot, Transaction


def _get_current_draft() -> Draft:
    draft = Draft.objects.order_by("-id").first()
    if not draft:
        raise ValidationError("Nenhum draft encontrado.")
    return draft


def is_player_free_agent(player: Player) -> bool:
    """
    Livre = não existe RosterSpot ativo (dropped_at is null) para esse player.
    """
    return not RosterSpot.objects.filter(player=player, dropped_at__isnull=True).exists()


def team_owns_player(team: Team, player: Player) -> bool:
    """
    True se o player está no roster ATIVO desse time.
    """
    return RosterSpot.objects.filter(team=team, player=player, dropped_at__isnull=True).exists()


@transaction.atomic
def drop_player_from_roster(team: Team, player: Player, *, note: str = "") -> None:
    """
    Marca dropped_at no spot ativo. Lança ValidationError se não estiver no time.
    """
    draft = _get_current_draft()

    spot = (
        RosterSpot.objects
        .select_for_update()
        .filter(draft=draft, team=team, player=player, dropped_at__isnull=True)
        .first()
    )
    if not spot:
        raise ValidationError("Drop inválido: jogador não está no roster ativo do time.")

    spot.dropped_at = timezone.now()
    spot.save(update_fields=["dropped_at"])

    Transaction.objects.create(
        draft=draft,
        team=team,
        player=player,
        type="DROP",
        notes=note or "WAIVER DROP",
    )


@transaction.atomic
def add_player_to_roster(team: Team, player: Player, *, acquired_via: str = "WAIVER", note: str = "") -> None:
    """
    Adiciona/re-ativa um spot do player para esse time.
    Bloqueia se o player já está ativo em qualquer time.
    """
    draft = _get_current_draft()

    # se já está em algum time, não pode
    active_spot = (
        RosterSpot.objects
        .select_for_update()
        .filter(draft=draft, player=player, dropped_at__isnull=True)
        .first()
    )
    if active_spot:
        raise ValidationError("Add inválido: jogador já pertence a um time.")

    spot = (
        RosterSpot.objects
        .select_for_update()
        .filter(draft=draft, player=player)
        .first()
    )

    if spot:
        spot.team = team
        spot.acquired_via = acquired_via
        spot.acquired_at = timezone.now()
        spot.dropped_at = None
        spot.save(update_fields=["team", "acquired_via", "acquired_at", "dropped_at"])
    else:
        RosterSpot.objects.create(
            draft=draft,
            team=team,
            player=player,
            acquired_via=acquired_via,
            acquired_at=timezone.now(),
        )

    Transaction.objects.create(
        draft=draft,
        team=team,
        player=player,
        type="ADD",
        notes=note or f"{acquired_via} ADD",
    )
