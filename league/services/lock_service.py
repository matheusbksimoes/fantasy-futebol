# league/services/lock_service.py

from league.models import PlayerWeekScore


def player_locked(player, round_number: int) -> bool:
    """
    Retorna True se o jogador já estiver com partida iniciada
    ou finalizada na rodada atual.
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

    return score.live_status in ["live", "finished"]