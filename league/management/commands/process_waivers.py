# league/management/commands/process_waivers.py
from django.core.management.base import BaseCommand
from django.db import transaction, models
from django.db.models import Max
from django.utils import timezone

from league.models import WaiverClaim, TeamBudget
from league.services.roster import (
    add_player_to_roster,
    drop_player_from_roster,
    is_player_free_agent,
    team_owns_player,
)


class Command(BaseCommand):
    help = "Processa waiver claims pendentes (FAAB)."

    def handle(self, *args, **options):
        self.stdout.write("Processando waivers...")

        total_processed = 0

        # Loop por “rodadas”:
        # em cada rodada, considera apenas 1 claim ATIVO por time (o mais antigo).
        # dentre os ativos, escolhe qual jogador processar primeiro por MAIOR BID.
        while True:
            active_rows = list(
                WaiverClaim.objects
                .filter(status=WaiverClaim.Status.PENDING)
                .values("id", "team_id", "add_player_id", "bid", "created_at")
                .order_by("team_id", "created_at", "id")
            )

            if not active_rows:
                break

            # 1º claim por time
            active_by_team = {}
            for r in active_rows:
                tid = r["team_id"]
                if tid not in active_by_team:
                    active_by_team[tid] = r

            active_claims = list(active_by_team.values())
            if not active_claims:
                break

            # escolhe o próximo jogador a processar (entre os ATIVOS)
            # prioridade: maior bid; empate: created_at mais antigo; empate final: add_player_id
            best = None
            for r in active_claims:
                key = (-int(r["bid"]), r["created_at"], int(r["add_player_id"]))
                if best is None or key < best[0]:
                    best = (key, r["add_player_id"])

            if not best:
                break

            add_player_id = best[1]
            total_processed += self._process_player_claims_active_only(add_player_id)

        self.stdout.write(self.style.SUCCESS(f"OK. Claims processados em {total_processed} linhas."))

    def _rotate_waiver_priority_after_tiebreak(self, winner_team_id: int):
        """
        Regra:
        - O time que levou jogador NO DESEMPATE vai para último na fila.
        - Todos que estavam atrás dele sobem uma posição (priority - 1).
        """
        winner_budget = TeamBudget.objects.select_for_update().get(team_id=winner_team_id)
        winner_prio = winner_budget.waiver_priority

        max_prio = TeamBudget.objects.aggregate(m=Max("waiver_priority"))["m"] or 0
        if max_prio < 1:
            max_prio = 1

        TeamBudget.objects.filter(waiver_priority__gt=winner_prio).update(
            waiver_priority=models.F("waiver_priority") - 1
        )

        winner_budget.waiver_priority = max_prio
        winner_budget.save(update_fields=["waiver_priority"])

    def _invalidate_other_claims_using_same_drop(self, winner: WaiverClaim, now):
        """
        Se o time ganhou um claim usando drop_player X,
        qualquer OUTRO claim PENDING do mesmo time usando o MESMO drop_player X
        vira INVALID (não dá pra dropar o mesmo jogador duas vezes).
        """
        if not winner.drop_player_id:
            return 0

        updated = (
            WaiverClaim.objects
            .select_for_update()
            .filter(
                status=WaiverClaim.Status.PENDING,
                team_id=winner.team_id,
                drop_player_id=winner.drop_player_id,
            )
            .exclude(id=winner.id)
            .update(
                status=WaiverClaim.Status.INVALID,
                invalid_reason="Você já usou este jogador como drop em outro claim vencedor",
                processed_at=now,
            )
        )
        return updated or 0

    def _process_player_claims_active_only(self, add_player_id: int) -> int:
        """
        Processa apenas os claims ATIVOS (1 por time) para um jogador específico.
        Retorna quantos claims foram marcados (WON/LOST/INVALID).
        """
        with transaction.atomic():
            now = timezone.now()

            # ✅ 1) Trava SOMENTE linhas da tabela WaiverClaim (SEM JOIN!)
            pending_locked = list(
                WaiverClaim.objects
                .select_for_update()
                .filter(status=WaiverClaim.Status.PENDING)
                .values("id", "team_id", "add_player_id", "created_at")
                .order_by("team_id", "created_at", "id")
            )

            if not pending_locked:
                return 0

            # ✅ 2) Calcula "ativo por time" em Python (primeiro claim pendente do time)
            first_by_team = {}
            for r in pending_locked:
                tid = r["team_id"]
                if tid not in first_by_team:
                    first_by_team[tid] = r

            # pega só os ativos cujo add_player_id é o jogador desta rodada
            active_ids_for_player = [
                r["id"]
                for r in first_by_team.values()
                if int(r["add_player_id"]) == int(add_player_id)
            ]

            if not active_ids_for_player:
                return 0

            # ✅ 3) Agora sim carrega objetos completos (pode ter JOIN, porque lock já foi feito)
            claims = list(
                WaiverClaim.objects
                .filter(id__in=active_ids_for_player)
                .select_related("team", "add_player", "drop_player")
            )

            if not claims:
                return 0

            # Se o jogador já não é FA, esses ativos viram INVALID
            if not is_player_free_agent(claims[0].add_player):
                for c in claims:
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Player is no longer a free agent"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # budgets com lock para os times envolvidos nesses claims ativos
            team_ids = {c.team_id for c in claims}
            budgets = {
                b.team_id: b
                for b in TeamBudget.objects.select_for_update().filter(team_id__in=team_ids)
            }
            for tid in team_ids:
                if tid not in budgets:
                    TeamBudget.objects.get_or_create(team_id=tid, defaults={"faab_balance": 100})
                    budgets[tid] = TeamBudget.objects.select_for_update().get(team_id=tid)

            # valida claims ativos
            valid_claims = []
            for c in claims:
                budget = budgets[c.team_id]

                if (budget.faab_balance or 0) < (c.bid or 0):
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Insufficient FAAB"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                    continue

                if c.drop_player_id and not team_owns_player(c.team, c.drop_player):
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Drop player not on team roster"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                    continue

                valid_claims.append(c)

            if not valid_claims:
                return len(claims)

            # winner: maior bid, empate: menor waiver_priority, depois created_at, depois id
            max_bid = max(int(c.bid or 0) for c in valid_claims)
            top = [c for c in valid_claims if int(c.bid or 0) == max_bid]

            used_tiebreak = False
            if len(top) == 1:
                winner = top[0]
            else:
                used_tiebreak = True
                winner = sorted(
                    top,
                    key=lambda c: (
                        int(budgets[c.team_id].waiver_priority or 10**9),
                        c.created_at,
                        c.id,
                    ),
                )[0]

            # re-check: ainda é FA
            if not is_player_free_agent(winner.add_player):
                for c in claims:
                    if c.status == WaiverClaim.Status.PENDING:
                        c.status = WaiverClaim.Status.INVALID
                        c.invalid_reason = "Player is no longer a free agent"
                        c.processed_at = now
                        c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # aplica vencedor
            winner_budget = budgets[winner.team_id]
            winner_budget.faab_balance = int(winner_budget.faab_balance or 0) - int(winner.bid or 0)
            winner_budget.save(update_fields=["faab_balance"])

            if winner.drop_player_id:
                drop_player_from_roster(winner.team, winner.drop_player)
            add_player_to_roster(winner.team, winner.add_player)

            winner.status = WaiverClaim.Status.WON
            winner.processed_at = now
            winner.invalid_reason = ""
            winner.save(update_fields=["status", "processed_at", "invalid_reason"])

            invalidated_count = self._invalidate_other_claims_using_same_drop(winner, now)

            if used_tiebreak:
                self._rotate_waiver_priority_after_tiebreak(winner.team_id)

            # restantes ATIVOS desse jogador: LOST (somente os que ainda estão PENDING)
            for c in claims:
                if c.id == winner.id:
                    continue
                if c.status == WaiverClaim.Status.PENDING:
                    c.status = WaiverClaim.Status.LOST
                    c.processed_at = now
                    c.save(update_fields=["status", "processed_at"])

            return len(claims) + invalidated_count
