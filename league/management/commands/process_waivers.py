# league/management/commands/process_waivers.py
from django.core.management.base import BaseCommand
from django.db import transaction, models
from django.db.models import Max, Min
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

        # ✅ Ordem global por MAIOR BID:
        # 1) agrupa por jogador (add_player_id)
        # 2) calcula o maior bid pendente daquele jogador (max_bid)
        # 3) processa primeiro os jogadores com max_bid maior
        # 4) empate entre jogadores: earliest_created_at e add_player_id (determinístico)
        player_groups = (
            WaiverClaim.objects
            .filter(status=WaiverClaim.Status.PENDING)
            .values("add_player_id")
            .annotate(
                max_bid=Max("bid"),
                earliest_created_at=Min("created_at"),
            )
            .order_by("-max_bid", "earliest_created_at", "add_player_id")
        )

        total_processed = 0

        for g in player_groups:
            total_processed += self._process_player_claims(g["add_player_id"])

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

        # Quem estava atrás do vencedor sobe 1 (prio -1)
        TeamBudget.objects.filter(waiver_priority__gt=winner_prio).update(
            waiver_priority=models.F("waiver_priority") - 1
        )

        # Vencedor vai pro final
        winner_budget.waiver_priority = max_prio
        winner_budget.save(update_fields=["waiver_priority"])

    def _invalidate_other_claims_using_same_drop(self, winner: WaiverClaim, now):
        """
        ✅ NOVA REGRA:
        Se o time ganhou um claim usando drop_player X,
        qualquer OUTRO claim PENDING do mesmo time usando o MESMO drop_player X
        deve virar INVALID (não dá pra dropar o mesmo jogador duas vezes).
        """
        if not winner.drop_player_id:
            return 0

        qs = (
            WaiverClaim.objects
            .select_for_update()
            .filter(
                status=WaiverClaim.Status.PENDING,
                team_id=winner.team_id,
                drop_player_id=winner.drop_player_id,
            )
            .exclude(id=winner.id)
        )

        updated = qs.update(
            status=WaiverClaim.Status.INVALID,
            invalid_reason="Drop player already used in another winning claim",
            processed_at=now,
        )
        return updated or 0

    def _process_player_claims(self, add_player_id: int) -> int:
        """
        Processa todos os claims PENDING para um jogador específico.
        Retorna quantos claims foram marcados (WON/LOST/INVALID).
        """
        with transaction.atomic():
            # Lock nos claims desse jogador (evita duas execuções concorrentes)
            claims = list(
                WaiverClaim.objects.select_for_update()
                .filter(status=WaiverClaim.Status.PENDING, add_player_id=add_player_id)
                .select_related("team", "add_player", "drop_player")
            )

            if not claims:
                return 0

            now = timezone.now()

            # Se o jogador já não é FA, todos são inválidos
            if not is_player_free_agent(claims[0].add_player):
                for c in claims:
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Player is no longer a free agent"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # ✅ garante budgets para todos os times (e faz lock)
            team_ids = {c.team_id for c in claims}
            budgets = {
                b.team_id: b
                for b in TeamBudget.objects.select_for_update().filter(team_id__in=team_ids)
            }
            for tid in team_ids:
                if tid not in budgets:
                    b, _ = TeamBudget.objects.get_or_create(team_id=tid)
                    # re-lock após criar
                    budgets[tid] = TeamBudget.objects.select_for_update().get(team_id=tid)

            # ✅ 1) valida e marca INVALID antes de escolher winner
            valid_claims = []
            for c in claims:
                budget = budgets[c.team_id]

                if budget.faab_balance < c.bid:
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
                # Se ninguém foi válido, os que restaram PENDING precisam virar INVALID
                for c in claims:
                    if c.status == WaiverClaim.Status.PENDING:
                        c.status = WaiverClaim.Status.INVALID
                        c.invalid_reason = "No valid claim for this player"
                        c.processed_at = now
                        c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # ✅ 2) escolher winner:
            # - maior bid
            # - empate: menor waiver_priority vence
            # - empate final: created_at mais antigo vence
            max_bid = max(c.bid for c in valid_claims)
            top = [c for c in valid_claims if c.bid == max_bid]

            used_tiebreak = False
            if len(top) == 1:
                winner = top[0]
            else:
                used_tiebreak = True
                winner = sorted(
                    top,
                    key=lambda c: (
                        budgets[c.team_id].waiver_priority,
                        c.created_at,
                    ),
                )[0]

            # ✅ 3) re-check: ainda é FA no momento de aplicar (concorrência)
            if not is_player_free_agent(winner.add_player):
                for c in claims:
                    if c.status == WaiverClaim.Status.PENDING:
                        c.status = WaiverClaim.Status.INVALID
                        c.invalid_reason = "Player is no longer a free agent"
                        c.processed_at = now
                        c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # ✅ 4) aplica vencedor (debita FAAB)
            winner_budget = budgets[winner.team_id]
            winner_budget.faab_balance -= winner.bid
            winner_budget.save(update_fields=["faab_balance"])

            # Drop (se houver) e Add
            if winner.drop_player_id:
                drop_player_from_roster(winner.team, winner.drop_player)
            add_player_to_roster(winner.team, winner.add_player)

            winner.status = WaiverClaim.Status.WON
            winner.processed_at = now
            winner.invalid_reason = ""
            winner.save(update_fields=["status", "processed_at", "invalid_reason"])

            # ✅ 4.1) NOVO: invalida outros claims do mesmo time que usam o MESMO drop
            invalidated_count = self._invalidate_other_claims_using_same_drop(winner, now)

            # ✅ 5) rotação de prioridade APENAS se venceu no desempate
            if used_tiebreak:
                self._rotate_waiver_priority_after_tiebreak(winner.team_id)

            # Restantes: LOST (exceto os já INVALID)
            for c in claims:
                if c.id == winner.id:
                    continue
                if c.status == WaiverClaim.Status.PENDING:
                    c.status = WaiverClaim.Status.LOST
                    c.processed_at = now
                    c.save(update_fields=["status", "processed_at"])

            # ✅ Observação:
            # os claims invalidados acima podem ser de OUTROS jogadores,
            # então eles não aparecem no "claims" atual; eles serão contados
            # quando aqueles jogadores forem processados (ou já aparecem em Transactions via processed_at).
            return len(claims) + invalidated_count
