from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from league.models import Draft, Week, Matchup, Team


def pair_key(a_id: int, b_id: int):
    return tuple(sorted((a_id, b_id)))


def round_pairs_key(round_pairs):
    """
    round_pairs: list of tuples (home_team, away_team)
    """
    return frozenset(pair_key(a.id, b.id) for (a, b) in round_pairs)


def circle_method_rounds(teams):
    """
    Retorna 9 rounds para 10 times (n par), cobrindo todos os pares 1x.
    Formato: list[ round ], onde round = list[(home, away)].
    """
    n = len(teams)
    assert n % 2 == 0, "Número de times precisa ser par para esta implementação."
    # método do círculo: fixa o primeiro, rotaciona o resto
    fixed = teams[0]
    rot = teams[1:]

    rounds = []
    for r in range(n - 1):  # 9 rounds
        left = [fixed] + rot[: (n // 2 - 1)]
        right = list(reversed(rot[(n // 2 - 1) :]))

        pairs = []
        for i in range(n // 2):
            a = left[i]
            b = right[i]
            # Alterna mando para dar uma “balanceada”
            if (r + i) % 2 == 0:
                pairs.append((a, b))
            else:
                pairs.append((b, a))

        rounds.append(pairs)

        # rotaciona
        rot = [rot[-1]] + rot[:-1]

    return rounds


def swap_home_away(rounds):
    return [[(b, a) for (a, b) in rnd] for rnd in rounds]


def build_27_rounds(teams):
    base9 = circle_method_rounds(teams)
    # 27 rounds = 3 turnos
    # Ciclo 1: base
    # Ciclo 2: inverte mando (opcional, mas ajuda)
    # Ciclo 3: base novamente
    return base9 + swap_home_away(base9) + base9


def find_schedule_matching_week1(teams, week1_pairset, max_shuffles=4000):
    """
    Tenta encontrar um schedule de 27 rounds cuja round 1 (após rotação)
    tenha os mesmos pares da week1 existente.
    """
    import random

    teams = list(teams)

    for attempt in range(max_shuffles + 1):
        if attempt > 0:
            random.shuffle(teams)

        schedule27 = build_27_rounds(teams)

        # achar em qual round do schedule os pares batem com week1
        idx = None
        for i, rnd in enumerate(schedule27):
            if round_pairs_key(rnd) == week1_pairset:
                idx = i
                break

        if idx is not None:
            # rotaciona para que week1 seja o round 1
            rotated = schedule27[idx:] + schedule27[:idx]
            return rotated

    return None


class Command(BaseCommand):
    help = "Gera Weeks 1..27 e matchups (round-robin 3x) respeitando Week 1 existente e setando Week 2 como atual."

    def add_arguments(self, parser):
        parser.add_argument("--draft-id", type=int, default=None, help="ID do Draft. Se omitido, usa o último.")
        parser.add_argument("--dry-run", action="store_true", help="Não grava no banco; só valida e mostra resumo.")
        parser.add_argument("--max-shuffles", type=int, default=4000, help="Tentativas de embaralhar times para encaixar Week 1.")

    @transaction.atomic
    def handle(self, *args, **opts):
        draft_id = opts["draft_id"]
        dry_run = opts["dry_run"]
        max_shuffles = opts["max_shuffles"]

        draft = Draft.objects.get(id=draft_id) if draft_id else Draft.objects.order_by("-id").first()
        if not draft:
            raise Exception("Não encontrei Draft.")

        teams = list(Team.objects.filter(league=draft.league).order_by("id"))
        if len(teams) != 10:
            raise Exception(f"Esperava 10 times, encontrei {len(teams)}.")

        # Week 1 precisa existir e ter 5 matchups
        try:
            week1 = Week.objects.get(draft=draft, number=1)
        except Week.DoesNotExist:
            raise Exception("Week 1 não existe. Crie Week 1 primeiro (ou ajuste o comando).")

        week1_matchups = list(Matchup.objects.filter(week=week1))
        if len(week1_matchups) != 5:
            raise Exception(f"Week 1 deveria ter 5 matchups. Encontrei {len(week1_matchups)}.")

        # ✅ AJUSTE PARA SEU MODEL: Matchup.home_team / Matchup.away_team
        week1_pairset = frozenset(
            pair_key(m.home_team_id, m.away_team_id)
            for m in week1_matchups
        )

        schedule27 = find_schedule_matching_week1(teams, week1_pairset, max_shuffles=max_shuffles)
        if schedule27 is None:
            raise Exception(
                "Não consegui gerar um calendário pelo método do círculo que contenha exatamente os pares da Week 1.\n"
                "Solução: ou recriar Week 1 via script, ou aumentar --max-shuffles, ou usar um gerador com backtracking."
            )

        # Garantias rápidas
        if round_pairs_key(schedule27[0]) != week1_pairset:
            raise Exception("Bug interno: schedule[0] não bate Week 1 após rotação.")

        # Criar Weeks 2..27 se não existirem
        weeks_by_number = {w.number: w for w in Week.objects.filter(draft=draft, number__in=range(1, 28))}
        created_weeks = 0
        for num in range(1, 28):
            if num not in weeks_by_number:
                if not dry_run:
                    weeks_by_number[num] = Week.objects.create(draft=draft, number=num, is_current=False)
                created_weeks += 1

        # Setar Week 2 como atual (e desligar as outras)
        if not dry_run:
            Week.objects.filter(draft=draft, is_current=True).update(is_current=False)
            Week.objects.filter(draft=draft, number=2).update(is_current=True)

        # Gerar matchups para Weeks 2..27 (Week 1 já existe)
        created_matchups = 0
        skipped_existing = 0

        for week_number in range(2, 28):
            week = weeks_by_number[week_number]
            rnd = schedule27[week_number - 1]  # week 1 usa index 0

            # Checagem: times não repetem na rodada
            used = set()
            for a, b in rnd:
                if a.id in used or b.id in used:
                    raise Exception(f"Conflito de times repetidos na Week {week_number}.")
                used.add(a.id)
                used.add(b.id)

            for home, away in rnd:
                # evita duplicar se já existe matchup nessa week com esse par (qualquer ordem)
                # ✅ AJUSTE PARA SEU MODEL: home_team / away_team
                exists = Matchup.objects.filter(week=week).filter(
                    (Q(home_team=home) & Q(away_team=away)) |
                    (Q(home_team=away) & Q(away_team=home))
                ).exists()

                if exists:
                    skipped_existing += 1
                    continue

                if not dry_run:
                    Matchup.objects.create(
                        week=week,
                        home_team=home,
                        away_team=away,
                    )
                created_matchups += 1

        msg = (
            f"Draft={draft.id} | Weeks criadas: {created_weeks} | "
            f"Matchups criados (Weeks 2..27): {created_matchups} | "
            f"Matchups já existentes e pulados: {skipped_existing} | "
            f"Week 2 setada como atual: {'NÃO (dry-run)' if dry_run else 'SIM'}"
        )
        self.stdout.write(self.style.SUCCESS(msg))

        if dry_run:
            # força rollback do atomic
            raise Exception("DRY-RUN finalizado (rollback intencional).")
