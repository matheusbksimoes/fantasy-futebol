from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Max, Min
from django.utils import timezone

from league.models import WaiverClaim, TeamBudget
from league.services.roster import is_player_free_agent, team_owns_player


class Command(BaseCommand):
    help = "DRY RUN: simula processamento de waivers (FAAB) sem gravar nada no banco."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Limita quantos jogadores (add_player_id) serão simulados (0 = todos).",
        )
        parser.add_argument(
            "--team",
            type=int,
            default=0,
            help="Filtra por team_id (0 = todos).",
        )
        parser.add_argument(
            "--player",
            type=int,
            default=0,
            help="Filtra por add_player_id (0 = todos).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        limit = int(options.get("limit") or 0)
        only_team_id = int(options.get("team") or 0)
        only_add_player_id = int(options.get("player") or 0)

        base_qs = WaiverClaim.objects.filter(status=WaiverClaim.Status.PENDING)

        if only_team_id:
            base_qs = base_qs.filter(team_id=only_team_id)
        if only_add_player_id:
            base_qs = base_qs.filter(add_player_id=only_add_player_id)

        # Ordem global por maior bid: agrupa por add_player_id
        player_groups = (
            base_qs
            .values("add_player_id")
            .annotate(
                max_bid=Max("bid"),
                earliest_created_at=Min("created_at"),
            )
            .order_by("-max_bid", "earliest_created_at", "add_player_id")
        )

        if limit and limit > 0:
            player_groups = player_groups[:limit]

        # Prefetch de budgets (para desempate por waiver_priority)
        team_ids = set(base_qs.values_list("team_id", flat=True))
        budgets = {
            b.team_id: b
            for b in TeamBudget.objects.filter(team_id__in=team_ids)
        }

        self.stdout.write(self.style.WARNING("=== DRY RUN: nenhuma mudança será salva no banco ==="))
        self.stdout.write(f"Total de teams com claims pendentes: {len(team_ids)}")

        # Estado simulado
        faab_sim = {
            tid: (budgets.get(tid).faab_balance if budgets.get(tid) else 100)
            for tid in team_ids
        }
        used_drop_by_team = defaultdict(set)  # team_id -> set(drop_player_id)

        simulated_results = []

        for idx, g in enumerate(player_groups, start=1):
            add_player_id = g["add_player_id"]
            max_bid = g["max_bid"]

            self.stdout.write("")
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"[{idx}] add_player_id={add_player_id} (max_bid={max_bid})"
                )
            )

            claims = list(
                WaiverClaim.objects
                .filter(status=WaiverClaim.Status.PENDING, add_player_id=add_player_id)
                .select_related("team", "add_player", "drop_player")
                .order_by("created_at", "id")
            )

            if not claims:
                self.stdout.write("  - Sem claims (pulando).")
                continue

            if not is_player_free_agent(claims[0].add_player):
                self.stdout.write(self.style.ERROR("  - Player não é FA (todos seriam INVALID)."))
                simulated_results.append((add_player_id, None, "NOOP", "Player not FA"))
                continue

            valid_claims = []
            invalid_reasons = {}

            for c in claims:
                team_id = c.team_id
                sim_balance = faab_sim.get(team_id, 0)

                if sim_balance < (c.bid or 0):
                    invalid_reasons[c.id] = "Insufficient FAAB (simulated)"
                    continue

                if c.drop_player_id and not team_owns_player(c.team, c.drop_player):
                    invalid_reasons[c.id] = "Drop player not on team roster"
                    continue

                # ✅ regra nova: mesmo drop não pode ser usado duas vezes pelo mesmo time
                if c.drop_player_id and c.drop_player_id in used_drop_by_team[team_id]:
                    invalid_reasons[c.id] = (
                        "Drop player already used in another winning claim (simulated)"
                    )
                    continue

                valid_claims.append(c)

            for c in claims:
                reason = invalid_reasons.get(c.id)
                if reason:
                    self.stdout.write(
                        f"  - Claim #{c.id} | team={c.team.name}({c.team_id}) | "
                        f"bid=${c.bid} -> INVALID [{reason}]"
                    )
                else:
                    self.stdout.write(
                        f"  - Claim #{c.id} | team={c.team.name}({c.team_id}) | bid=${c.bid} -> OK"
                    )

            if not valid_claims:
                self.stdout.write(self.style.ERROR("  => Resultado: nenhum claim válido (sem WIN)."))
                simulated_results.append((add_player_id, None, "NO_WIN", "No valid claims"))
                continue

            max_bid2 = max(c.bid for c in valid_claims)
            top = [c for c in valid_claims if c.bid == max_bid2]

            used_tiebreak = False
            if len(top) == 1:
                winner = top[0]
            else:
                used_tiebreak = True

                def prio_for(team_id: int):
                    b = budgets.get(team_id)
                    return b.waiver_priority if (b and b.waiver_priority is not None) else 999999

                winner = sorted(
                    top,
                    key=lambda c: (prio_for(c.team_id), c.created_at, c.id),
                )[0]

            faab_sim[winner.team_id] -= winner.bid or 0

            if winner.drop_player_id:
                used_drop_by_team[winner.team_id].add(winner.drop_player_id)

            self.stdout.write(
                self.style.SUCCESS(
                    f"  => WINNER (simulado): claim #{winner.id} | team={winner.team.name} "
                    f"| bid=${winner.bid} {'| (tiebreak)' if used_tiebreak else ''}"
                )
            )
            self.stdout.write(
                f"     FAAB simulado restante do time: ${faab_sim[winner.team_id]}"
            )

            simulated_results.append((add_player_id, winner.id, "WIN", "OK"))

        self.stdout.write("")
        self.stdout.write(self.style.WARNING("=== FIM DO DRY RUN ==="))
        self.stdout.write("Nada foi salvo no banco.")
