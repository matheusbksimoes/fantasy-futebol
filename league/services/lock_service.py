from django.utils import timezone

from league.models import PlayerWeekScore


def player_played(score: PlayerWeekScore) -> bool:
    points = score.points or 0

    try:
        if float(points) != 0:
            return True
    except (TypeError, ValueError):
        pass

    scouts = score.scouts or {}

    if isinstance(scouts, dict):
        for value in scouts.values():
            try:
                if float(value or 0) != 0:
                    return True
            except (TypeError, ValueError):
                continue

    return False


def player_locked(player, round_number: int) -> bool:
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

    if score.live_status == "finished":
        return player_played(score)

    if score.live_status == "live":
        return True

    if score.match_started_at and score.match_started_at <= timezone.now():
        return True

    return False