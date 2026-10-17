"""
Chaveamento dos playoffs.

Formato definido com o dono da liga:
- 6 times classificados pela tabela da fase regular
- seeds 1 e 2 folgam a 1ª fase (3º x 6º e 4º x 5º)
- 1ª fase e semifinais valem a soma de 2 rodadas (ida e volta, soma dos pontos)
- a final é em jogo único (1 rodada)
- empate no agregado: avança quem fez mais pontos na temporada (fase regular)
- os confrontos são criados no admin (Matchup em semanas com is_playoff=True);
  aqui só lemos e organizamos para exibição

As semanas de playoff (is_playoff=True, ordenadas por número) são distribuídas
pelas fases: as 2 primeiras formam a 1ª fase, as 2 seguintes as semifinais e a
última a final (jogo único). Total: 5 semanas de playoff.
"""
from collections import defaultdict

from league.models import Matchup, Week

PLAYOFF_TEAMS = 6
BYE_SEEDS = (1, 2)
PHASES = (
    {"key": "round1", "label": "1ª fase", "legs": 2},
    {"key": "semis", "label": "Semifinais", "legs": 2},
    {"key": "final", "label": "Final", "legs": 1},
)


def _weeks_label(weeks):
    numbers = [w.number for w in weeks]
    if len(numbers) >= 2:
        return f"Rodadas {numbers[0]}–{numbers[-1]}"
    if numbers:
        return f"Rodada {numbers[0]}"
    return "Datas a definir"


def build_playoff_bracket(draft, standings, current_week=None):
    """
    Monta os dados do chaveamento a partir dos Matchups das semanas de playoff.

    `standings` precisa vir só da fase regular (build_standings já faz isso):
    é dela que saem os seeds e o critério de desempate (pontos na temporada).
    """
    rank_by_team = {row["team"].id: row["rank"] for row in standings}
    pf_by_team = {row["team"].id: float(row["points_for"]) for row in standings}

    def seed_of(team_id):
        rank = rank_by_team.get(team_id)
        return rank if rank is not None and rank <= PLAYOFF_TEAMS else None

    qualifiers = [
        {
            "team": row["team"],
            "seed": row["rank"],
            "bye": row["rank"] in BYE_SEEDS,
            "wins": row["wins"],
            "losses": row["losses"],
            "ties": row["ties"],
            "points_for": round(float(row["points_for"]), 2),
        }
        for row in standings[:PLAYOFF_TEAMS]
    ]

    playoff_weeks = list(
        Week.objects.filter(draft=draft, is_playoff=True).order_by("number")
    )

    matchups_by_week = defaultdict(list)
    if playoff_weeks:
        matchups = (
            Matchup.objects
            .filter(week__in=playoff_weeks)
            .select_related("home_team", "away_team", "week")
            .order_by("week__number", "id")
        )
        for m in matchups:
            matchups_by_week[m.week_id].append(m)

    def leg_finished(m):
        if m.is_final:
            return True
        return bool(current_week and m.week.number < current_week.number)

    phases = []
    offset = 0

    for index, meta in enumerate(PHASES):
        legs_needed = meta["legs"]
        weeks = playoff_weeks[offset:offset + legs_needed]
        offset += legs_needed
        ties = {}

        for leg_number, week in enumerate(weeks, start=1):
            for m in matchups_by_week.get(week.id, []):
                key = frozenset((m.home_team_id, m.away_team_id))
                tie = ties.get(key)

                if tie is None:
                    team_a, team_b = sorted(
                        (m.home_team, m.away_team),
                        key=lambda t: (seed_of(t.id) or 999, t.id),
                    )
                    tie = {
                        "team_a": team_a,
                        "team_b": team_b,
                        "seed_a": seed_of(team_a.id),
                        "seed_b": seed_of(team_b.id),
                        "legs": [],
                        "total_a": 0.0,
                        "total_b": 0.0,
                    }
                    ties[key] = tie

                home_pts = float(m.home_score or 0)
                away_pts = float(m.away_score or 0)

                # a soma é por time, não por mando (o mando pode inverter na volta)
                if m.home_team_id == tie["team_a"].id:
                    a_pts, b_pts = home_pts, away_pts
                else:
                    a_pts, b_pts = away_pts, home_pts

                tie["legs"].append({
                    "leg": leg_number,
                    "week": week,
                    "matchup": m,
                    "a_points": a_pts,
                    "b_points": b_pts,
                    "finished": leg_finished(m),
                })
                tie["total_a"] += a_pts
                tie["total_b"] += b_pts

        for tie in ties.values():
            tie["total_a"] = round(tie["total_a"], 2)
            tie["total_b"] = round(tie["total_b"], 2)

            tie["decided"] = (
                len(weeks) == legs_needed
                and len(tie["legs"]) == legs_needed
                and all(leg["finished"] for leg in tie["legs"])
            )
            tie["winner"] = None
            tie["decided_by_tiebreak"] = False

            if tie["decided"]:
                a, b = tie["team_a"], tie["team_b"]

                if tie["total_a"] != tie["total_b"]:
                    winner = a if tie["total_a"] > tie["total_b"] else b
                else:
                    tie["decided_by_tiebreak"] = True
                    pf_a = pf_by_team.get(a.id, 0.0)
                    pf_b = pf_by_team.get(b.id, 0.0)
                    if pf_a != pf_b:
                        winner = a if pf_a > pf_b else b
                    else:
                        winner = a  # team_a é o seed melhor
                tie["winner"] = winner

        ordered_ties = sorted(
            ties.values(),
            key=lambda t: (t["seed_a"] or 999, t["seed_b"] or 999),
        )

        if any(w.is_current for w in weeks):
            status, status_label = "current", "Em andamento"
        elif ordered_ties and all(t["decided"] for t in ordered_ties):
            status, status_label = "finished", "Finalizada"
        elif weeks and current_week and all(w.number < current_week.number for w in weeks):
            status, status_label = "finished", "Finalizada"
        else:
            status, status_label = "upcoming", "Em breve"

        phases.append({
            "key": meta["key"],
            "label": meta["label"],
            "legs_needed": legs_needed,
            "weeks": weeks,
            "weeks_label": _weeks_label(weeks),
            "has_weeks": bool(weeks),
            "ties": ordered_ties,
            "byes": [q for q in qualifiers if q["bye"]] if index == 0 else [],
            "status": status,
            "status_label": status_label,
        })

    champion = None
    final_ties = phases[-1]["ties"]
    if len(final_ties) == 1 and final_ties[0]["decided"]:
        champion = final_ties[0]["winner"]

    return {
        "configured": bool(playoff_weeks),
        "qualifiers": qualifiers,
        "phases": phases,
        "champion": champion,
    }