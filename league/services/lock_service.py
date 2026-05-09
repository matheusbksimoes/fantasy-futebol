from django.utils import timezone

from league.models import PlayerWeekScore


def player_locked(player, round_number: int) -> bool:
    """
    Retorna True se o jogador já estiver travado
    (partida iniciada ou encerrada).
    """

    score = (
        PlayerWeekScore.objects
        .filter(
            week__number=round_number,
            player=player,
        )
        .first()
    )

    if not score:
        return False

    # Lock imediato por status live/finished
    if score.live_status in ["live", "finished"]:
        return True

    # Lock por horário real da partida
    if score.match_started_at:
        return score.match_started_at <= timezone.now()

    return False