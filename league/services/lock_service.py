# league/services/lock_service.py

def player_locked(player, round_number: int) -> bool:
    """
    Retorna True se o jogador estiver 'travado' (jogo já começou / em andamento) na rodada.
    Por enquanto, default = False (ninguém travado), para destravar o desenvolvimento.

    Depois você liga isso no que o sync_cartola_scores já salva (ex: PlayerRound/Score/Status).
    """
    return False
