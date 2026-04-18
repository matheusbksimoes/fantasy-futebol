from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Avg, Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    Draft,
    DraftPick,
    Team,
    Player,
    RosterSpot,
    Transaction,
    Week,
    Matchup,
    Lineup,
    LineupSpot,
    PlayerWeekScore,
    FORMATION_MAP,
    TeamBudget,
    WaiverClaim,
    TradeProposal,
    TradeItem,
    Notification,
)

from league.services.lock_service import player_locked
# ============================================================
# 🔒 Permissões: só dono do time (ou admin) pode mexer no time
# ============================================================
def forbid_if_not_team_owner(request, team: Team):
    """
    Admin pode tudo.
    Usuário normal só pode mexer no próprio time.
    Se o time não está vinculado a um usuário, bloqueia (exceto admin).
    """
    if request.user.is_superuser:
        return None

    if team.user_id is None:
        return HttpResponseForbidden("Este time ainda não está vinculado a um usuário.")

    if team.user_id != request.user.id:
        return HttpResponseForbidden("Você não tem permissão para mexer neste time.")

    return None


# ============================================================
# Draft board
# ============================================================
@login_required
def draft_board(request, draft_id: int):
    draft = get_object_or_404(Draft, id=draft_id)

    teams = list(Team.objects.filter(league=draft.league).order_by("id"))

    picks_qs = (
        DraftPick.objects
        .filter(draft=draft)
        .select_related("team", "player")
        .order_by("overall_number")
    )

    current_pick = (
        DraftPick.objects
        .filter(draft=draft, is_current=True)
        .select_related("team", "player")
        .first()
    )

    can_draft = False
    if current_pick:
        if request.user.is_superuser:
            can_draft = True
        elif current_pick.team.user_id == request.user.id:
            can_draft = True

    rounds = draft.rounds
    board_rows = []
    picks = list(picks_qs)
    pick_map = {(p.round_number, p.team_id): p for p in picks}

    for r in range(1, rounds + 1):
        row = []
        for t in teams:
            row.append(pick_map.get((r, t.id)))
        board_rows.append(row)

    current_week = Week.objects.filter(draft=draft, is_current=True).first()

    return render(request, "league/draft_board.html", {
        "draft": draft,
        "teams": teams,
        "board_rows": board_rows,
        "current_pick": current_pick,
        "can_draft": can_draft,
        "current_week": current_week,
        "week": current_week,          # pro base.html
        "team": None,                  # base.html não quebra
        "active_tab": "draft",
    })


@login_required
@transaction.atomic
def start_draft_view(request, draft_id: int):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Apenas o admin pode iniciar o draft.")

    draft = get_object_or_404(Draft, id=draft_id)

    DraftPick.objects.filter(draft=draft).update(is_current=False)

    first_pick = (
        DraftPick.objects
        .filter(draft=draft, player__isnull=True)
        .order_by("overall_number")
        .first()
    )

    if first_pick:
        first_pick.is_current = True
        first_pick.save(update_fields=["is_current"])

    return redirect("draft_board", draft_id=draft.id)


@login_required
@transaction.atomic
def make_pick(request, draft_id: int):
    draft = get_object_or_404(Draft, id=draft_id)

    current_pick = (
        DraftPick.objects
        .select_related("team", "player")
        .filter(draft=draft, is_current=True)
        .first()
    )

    if not current_pick:
        return render(request, "league/make_pick.html", {
            "draft": draft,
            "current_pick": None,
            "available_players": [],
            "error": "Não há pick atual (draft não iniciado ou já terminou).",
            "teams": [],
            "q": "",
            "pos": "",
            "team": "",
        })

    # Permissão: só admin ou dono do time da vez
    if not request.user.is_superuser:
        if current_pick.team.user_id is None or current_pick.team.user_id != request.user.id:
            return HttpResponseForbidden(
                "Você não está na vez (ou o time ainda não está vinculado a um usuário)."
            )

    # filtros (GET)
    query = request.GET.get("q", "").strip()
    pos = request.GET.get("pos", "").strip()
    real_team = request.GET.get("team", "").strip()

    drafted_ids = (
        DraftPick.objects
        .filter(draft=draft, player__isnull=False)
        .values_list("player_id", flat=True)
    )

    base_qs = Player.objects.exclude(id__in=drafted_ids)

    teams_filter = (
        base_qs.exclude(real_team__isnull=True)
        .exclude(real_team__exact="")
        .values_list("real_team", flat=True)
        .distinct()
        .order_by("real_team")
    )

    available_players = base_qs
    if pos in {"GOL", "LAT", "ZAG", "MEI", "ATA", "TEC"}:
        available_players = available_players.filter(position=pos)
    if real_team:
        available_players = available_players.filter(real_team=real_team)
    if query:
        available_players = available_players.filter(name__icontains=query)

    available_players = available_players.order_by("name")

    if request.method == "POST":
        player_id = request.POST.get("player_id")
        if not player_id:
            return render(request, "league/make_pick.html", {
                "draft": draft,
                "current_pick": current_pick,
                "available_players": available_players,
                "teams": teams_filter,
                "q": query,
                "pos": pos,
                "team": real_team,
                "error": "Selecione um jogador.",
            })

        player = get_object_or_404(Player, id=player_id)

        if DraftPick.objects.filter(draft=draft, player=player).exists():
            return render(request, "league/make_pick.html", {
                "draft": draft,
                "current_pick": current_pick,
                "available_players": available_players,
                "teams": teams_filter,
                "q": query,
                "pos": pos,
                "team": real_team,
                "error": "Esse jogador já foi draftado.",
            })

        # salvar pick
        current_pick.player = player
        current_pick.made_at = timezone.now()
        current_pick.is_current = False
        current_pick.save()

        # criar/reativar roster spot (DRAFT)
        spot, created = RosterSpot.objects.get_or_create(
            draft=draft,
            player=player,
            defaults={
                "team": current_pick.team,
                "acquired_via": "DRAFT",
                "acquired_at": timezone.now(),
            },
        )
        if not created:
            spot.team = current_pick.team
            spot.acquired_via = "DRAFT"
            spot.acquired_at = timezone.now()
            spot.dropped_at = None
            spot.save()

        # registrar transação (DRAFT)
        Transaction.objects.create(
            draft=draft,
            team=current_pick.team,
            player=player,
            type="DRAFT",
        )

        # avançar para o próximo pick vazio
        next_pick = (
            DraftPick.objects
            .filter(draft=draft, player__isnull=True)
            .order_by("overall_number")
            .first()
        )
        if next_pick:
            next_pick.is_current = True
            next_pick.save(update_fields=["is_current"])

        return redirect("draft_board", draft_id=draft.id)

    return render(request, "league/make_pick.html", {
        "draft": draft,
        "current_pick": current_pick,
        "available_players": available_players,
        "teams": teams_filter,
        "q": query,
        "pos": pos,
        "team": real_team,
        "error": None,
    })


# ============================================================
# Roster / Free agents / Transactions
# ============================================================
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Avg
from django.shortcuts import get_object_or_404, render

from .models import Draft, Team, Week, RosterSpot, PlayerWeekScore, TeamBudget


@login_required
def team_roster(request, draft_id: int, team_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    viewed_team = get_object_or_404(Team, id=team_id)

    week = Week.objects.filter(draft=draft, is_current=True).first()

    my_team = Team.objects.filter(
        league=draft.league,
        user=request.user,
    ).first()

    can_manage_team = bool(my_team and my_team.id == viewed_team.id)

    roster_spots = list(
        RosterSpot.objects
        .select_related("player")
        .filter(draft=draft, team=viewed_team, dropped_at__isnull=True)
        .order_by("manual_order", "id")
    )

    my_matchup = get_team_matchup_for_week(week, my_team) if week and my_team else None

    budget_obj, _ = TeamBudget.objects.get_or_create(team=viewed_team)
    if budget_obj.faab_balance is None:
        budget_obj.faab_balance = viewed_team.budget
        budget_obj.save(update_fields=["faab_balance"])

    faab_balance = budget_obj.faab_balance

    roster_items = []

    for spot in roster_spots:
        player = spot.player

        stats = PlayerWeekScore.objects.filter(
            player=player,
            week__draft=draft,
        )

        aggregate_stats = stats.aggregate(
            total=Sum("points"),
            avg=Avg("points"),
        )

        last_3_games = list(
            stats.select_related("week")
            .order_by("-week__number")[:3]
        )

        last_3_points = [
            game.points for game in last_3_games
            if game.points is not None
        ]

        last_3_avg = None
        if last_3_points:
            last_3_avg = round(sum(last_3_points) / len(last_3_points), 2)

        season_avg = aggregate_stats["avg"]
        if season_avg is not None:
            season_avg = round(season_avg, 2)

        total_points = aggregate_stats["total"]
        if total_points is not None:
            total_points = round(total_points, 2)

        last_game_points = last_3_points[0] if last_3_points else None

        is_hot = False
        if last_3_avg is not None and season_avg is not None:
            is_hot = last_3_avg > season_avg

        current_game = None
        fixture_label = None

        if week:
            current_game = stats.filter(week=week).first()

        if current_game and current_game.match_display:
            if current_game.is_home is True:
                mando = "Casa"
            elif current_game.is_home is False:
                mando = "Fora"
            else:
                mando = None

            fixture_label = current_game.match_display
            if mando:
                fixture_label = f"{fixture_label} • {mando}"

        roster_items.append({
            "spot": spot,
            "player": player,
            "stats": {
                "total": total_points,
                "avg": season_avg,
                "last_3_avg": last_3_avg,
                "last_game_points": last_game_points,
                "is_hot": is_hot,
                "last_3_games": last_3_games,
            },
            "current_fixture": fixture_label,
        })

    return render(request, "league/team_roster.html", {
        "team": viewed_team,
        "my_team": my_team,
        "draft": draft,
        "week": week,
        "roster_spots": roster_spots,
        "roster_items": roster_items,
        "active_tab": "roster",
        "can_manage_team": can_manage_team,
        "my_matchup": my_matchup,
        "faab_balance": faab_balance,
    })

@login_required
@require_POST
def drop_player(request, team_id: int, player_id: int):
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    player = get_object_or_404(Player, id=player_id)

    spot = get_object_or_404(
        RosterSpot,
        team=team,
        player=player,
        dropped_at__isnull=True
    )

    spot.dropped_at = timezone.now()
    spot.save(update_fields=["dropped_at"])

    messages.success(request, f"{player.name} foi dropado do {team.name}.")
    draft = Draft.objects.order_by("-id").first()
    return redirect("team_roster", draft_id=draft.id, team_id=team.id)


@login_required
def free_agents_list(request, team_id: int):
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    draft = Draft.objects.order_by("-id").first()
    if not draft:
        return render(request, "league/free_agents.html", {
            "team": team,
            "draft": None,
            "week": None,
            "free_agents": [],
            "active_tab": "free_agents",
            "faab_balance": 100,
            "pending_claims_count": {},
            "pending_add_ids": set(),
            "active_roster": [],
            "player_stats": {},
        })

    week = Week.objects.filter(draft=draft, is_current=True).first()

    active_player_ids = (
        RosterSpot.objects
        .filter(dropped_at__isnull=True)
        .values_list("player_id", flat=True)
    )

    free_agents = Player.objects.exclude(id__in=active_player_ids).order_by("name")

    budget, _ = TeamBudget.objects.get_or_create(team=team)
    if budget.faab_balance is None:
        budget.faab_balance = team.budget
        budget.save(update_fields=["faab_balance"])

    pending_claims_count = {
        row["add_player_id"]: row["cnt"]
        for row in (
            WaiverClaim.objects
            .filter(team=team, status=WaiverClaim.Status.PENDING)
            .values("add_player_id")
            .annotate(cnt=models.Count("id"))
        )
    }

    pending_add_ids = set(pending_claims_count.keys())

    active_roster = (
        RosterSpot.objects
        .filter(draft=draft, team=team, dropped_at__isnull=True)
        .select_related("player")
        .order_by("player__position", "player__name")
    )

    player_stats = {}

    for p in free_agents:
        scores_qs = (
            PlayerWeekScore.objects
            .filter(player=p, week__draft=draft)
            .select_related("week")
            .order_by("week__number")
        )

        aggregate_stats = scores_qs.aggregate(
            total=Sum("points"),
            avg=Avg("points"),
        )

        recent_scores = list(scores_qs.order_by("-week__number")[:3])
        recent_scores.reverse()

        if recent_scores:
            recent_avg = sum(float(s.points) for s in recent_scores) / len(recent_scores)
        else:
            recent_avg = 0.0

        season_avg = float(aggregate_stats["avg"] or 0)
        trend_delta = recent_avg - season_avg

        if recent_scores and trend_delta >= 0.75:
            trend = "alta"
        elif recent_scores and trend_delta <= -0.75:
            trend = "baixa"
        else:
            trend = "estavel"

        player_stats[p.id] = {
            "avg": aggregate_stats["avg"] or 0,
            "total": aggregate_stats["total"] or 0,
            "recent_avg": recent_avg,
            "trend": trend,
            "trend_delta": trend_delta,
        }

    return render(request, "league/free_agents.html", {
        "team": team,
        "draft": draft,
        "week": week,
        "free_agents": free_agents,
        "active_tab": "free_agents",
        "faab_balance": budget.faab_balance or 0,
        "pending_claims_count": pending_claims_count,
        "pending_add_ids": pending_add_ids,
        "active_roster": active_roster,
        "player_stats": player_stats,
    })

@login_required
@require_POST
def add_free_agent(request, team_id: int, player_id: int):
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    player = get_object_or_404(Player, id=player_id)

    draft = Draft.objects.order_by("-id").first()
    if not draft:
        messages.error(request, "Nenhum draft encontrado.")
        return redirect("free_agents_list", team_id=team.id)

    # Se já pertence a alguém, não cria claim
    active_spot = RosterSpot.objects.filter(
        draft=draft,
        player=player,
        dropped_at__isnull=True
    ).first()
    if active_spot:
        messages.error(request, f"{player.name} já pertence a um time.")
        return redirect("free_agents_list", team_id=team.id)

    # Bid
    try:
        bid = int(request.POST.get("bid", 0))
    except ValueError:
        bid = 0
    bid = max(bid, 0)

    # Drop
    drop_player_id = request.POST.get("drop_player_id") or None
    drop_player = None
    if drop_player_id:
        drop_player = get_object_or_404(Player, id=drop_player_id)

        owns_drop = RosterSpot.objects.filter(
            draft=draft,
            team=team,
            player=drop_player,
            dropped_at__isnull=True
        ).exists()
        if not owns_drop:
            messages.error(request, "O jogador selecionado para drop não está no seu roster.")
            return redirect("free_agents_list", team_id=team.id)

        if drop_player.id == player.id:
            messages.error(request, "Você não pode dropar o mesmo jogador que está tentando adicionar.")
            return redirect("free_agents_list", team_id=team.id)

    # ✅ valida FAAB do time (impede bid maior que saldo)
    budget, _ = TeamBudget.objects.get_or_create(team=team)
    if budget.faab_balance is None:
        budget.faab_balance = 100
        budget.save(update_fields=["faab_balance"])

    if bid > budget.faab_balance:
        messages.error(request, f"FAAB insuficiente. Seu saldo: ${budget.faab_balance}.")
        return redirect("free_agents_list", team_id=team.id)

    # ✅ cria claim
    try:
        WaiverClaim.objects.create(
            team=team,
            add_player=player,
            drop_player=drop_player,
            bid=bid,
            status=WaiverClaim.Status.PENDING,
        )
    except IntegrityError:
        # ⚠️ enquanto a constraint existir, o 2º claim pro mesmo jogador estoura aqui.
        messages.error(
            request,
            "Você já tem um claim pendente para este jogador. (Ainda existe uma trava no banco; precisa remover a constraint pra permitir múltiplos lances.)"
        )
        return redirect("free_agents_list", team_id=team.id)

    messages.success(request, f"Claim criado para {player.name} (bid ${bid}).")
    return redirect("free_agents_list", team_id=team.id)


@login_required
def team_roster_legacy(request, team_id: int):
    draft = Draft.objects.order_by("-id").first()
    if not draft:
        return HttpResponseForbidden("Nenhum draft encontrado.")
    return redirect("team_roster", draft_id=draft.id, team_id=team_id)


from collections import defaultdict
from types import SimpleNamespace
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from .models import Draft, Week, Transaction, WaiverClaim


@login_required
def transactions_list(request, draft_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    week = Week.objects.filter(draft=draft, is_current=True).first()

    # Transações "normais" (FA, DROP, DRAFT etc) em ordem cronológica
    transactions = (
        Transaction.objects
        .filter(draft=draft)
        .select_related("team", "player")
        .order_by("-created_at")[:200]
    )

    # Waivers já processados (WON/LOST/INVALID/CANCELLED)
    waiver_claims = (
        WaiverClaim.objects
        .exclude(status=WaiverClaim.Status.PENDING)
        .select_related("team", "add_player", "drop_player")
        .order_by("add_player__name", "-bid", "team__name", "-processed_at", "-id")[:800]
    )

    # Agrupa por jogador alvo (add_player)
    grouped = defaultdict(list)
    for c in waiver_claims:
        when = c.processed_at or c.created_at or timezone.now()

        grouped[c.add_player_id].append(SimpleNamespace(
            player=c.add_player,
            team=c.team,
            status=c.status,
            bid=c.bid,
            drop_player=c.drop_player,
            invalid_reason=(c.invalid_reason or ""),
            when=when,
        ))

    # transforma em lista ordenada por nome do jogador
    waiver_groups = []
    for add_player_id, rows in grouped.items():
        rows.sort(key=lambda r: (-int(r.bid or 0), (r.team.name or ""), -(r.when.timestamp() if r.when else 0)))
        waiver_groups.append({
            "player": rows[0].player,
            "rows": rows,
        })

    waiver_groups.sort(key=lambda g: (g["player"].name or ""))

    return render(request, "league/transactions.html", {
        "draft": draft,
        "week": week,
        "team": None,
        "transactions": transactions,
        "waiver_groups": waiver_groups,
        "active_tab": "transactions",
    })





# ============================================================
# Compat: criar waiver claim (não usado no urls.py atual, mas mantido)
# ============================================================
@require_POST
@login_required
def create_waiver_claim(request, team_id: int, player_id: int):
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    add_player = get_object_or_404(Player, id=player_id)

    try:
        bid = int(request.POST.get("bid", 0))
    except ValueError:
        bid = 0
    if bid < 0:
        bid = 0

    drop_player_id = request.POST.get("drop_player_id") or None
    drop_player = get_object_or_404(Player, id=drop_player_id) if drop_player_id else None

    WaiverClaim.objects.create(
        team=team,
        add_player=add_player,
        drop_player=drop_player,
        bid=bid,
        status=WaiverClaim.Status.PENDING,
    )

    messages.success(request, "Waiver claim criado! Vai ser processado no próximo horário.")
    return redirect("free_agents_list", team_id=team.id)


# ============================================================
# ✅ MEUS CLAIMS (listar / editar / cancelar)  (usado no urls.py)
# ============================================================
@login_required
def my_claims(request, team_id: int):
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    draft = Draft.objects.order_by("-id").first()
    week = Week.objects.filter(draft=draft, is_current=True).first() if draft else None

    # FAAB
    budget, _ = TeamBudget.objects.get_or_create(team=team)
    if budget.faab_balance is None:
        budget.faab_balance = 100
        budget.save(update_fields=["faab_balance"])

    # ✅ Ordena por maior bid -> menor
    claims = (
        WaiverClaim.objects
        .filter(team=team, status=WaiverClaim.Status.PENDING)
        .select_related("add_player", "drop_player")
        .order_by("-bid", "created_at", "id")
    )

    # roster ativo (pra dropdown de drop)
    active_roster = []
    if draft:
        active_roster = (
            RosterSpot.objects
            .filter(draft=draft, team=team, dropped_at__isnull=True)
            .select_related("player")
            .order_by("player__position", "player__name")
        )

    return render(request, "league/my_claims.html", {
        "draft": draft,
        "week": week,
        "team": team,
        "active_tab": "my_claims",
        "claims": claims,
        "active_roster": active_roster,
        "faab_balance": budget.faab_balance or 0,
    })


@login_required
@require_POST
@transaction.atomic
def update_claim(request, team_id: int, claim_id: int):
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    claim = get_object_or_404(WaiverClaim, id=claim_id, team=team)

    if claim.status != WaiverClaim.Status.PENDING:
        messages.error(request, "Só é possível editar claims pendentes.")
        return redirect("my_claims", team_id=team.id)

    draft = Draft.objects.order_by("-id").first()

    # bid
    try:
        bid = int(request.POST.get("bid", claim.bid or 0))
    except ValueError:
        bid = claim.bid or 0
    if bid < 0:
        bid = 0

    budget, _ = TeamBudget.objects.get_or_create(team=team)
    if budget.faab_balance is None:
        budget.faab_balance = 100
        budget.save(update_fields=["faab_balance"])

    if bid > (budget.faab_balance or 0):
        messages.error(request, f"FAAB insuficiente. Seu saldo: ${budget.faab_balance}.")
        return redirect("my_claims", team_id=team.id)

    # drop
    drop_player_id = request.POST.get("drop_player_id") or ""
    drop_player = None
    if drop_player_id.strip():
        drop_player = get_object_or_404(Player, id=int(drop_player_id))

        if not draft:
            messages.error(request, "Nenhum draft encontrado para validar drop.")
            return redirect("my_claims", team_id=team.id)

        owns_drop = RosterSpot.objects.filter(
            draft=draft,
            team=team,
            player=drop_player,
            dropped_at__isnull=True
        ).exists()
        if not owns_drop:
            messages.error(request, "O jogador selecionado para drop não está no seu roster.")
            return redirect("my_claims", team_id=team.id)

        if drop_player.id == claim.add_player_id:
            messages.error(request, "Você não pode dropar o mesmo jogador que está tentando adicionar.")
            return redirect("my_claims", team_id=team.id)

    claim.bid = bid
    claim.drop_player = drop_player
    # se você tem updated_at no model, ok; se não tiver, remova updated_at daqui
    try:
        claim.save(update_fields=["bid", "drop_player", "updated_at"])
    except Exception:
        claim.save(update_fields=["bid", "drop_player"])

    messages.success(request, f"Claim atualizado: {claim.add_player.name} (bid ${bid}).")
    return redirect("my_claims", team_id=team.id)


@login_required
@require_POST
def cancel_claim(request, team_id: int, claim_id: int):
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    claim = get_object_or_404(WaiverClaim, id=claim_id, team=team)

    if claim.status != WaiverClaim.Status.PENDING:
        messages.error(request, "Você só pode cancelar claims pendentes.")
        return redirect("my_claims", team_id=team.id)

    claim.delete()
    messages.success(request, "Claim cancelado.")
    return redirect("my_claims", team_id=team.id)


# ============================================================
# ✅ ESCALAÇÃO (Week + Lineup + Formation + LineupSpot indexado)
# ============================================================
SLOT_ORDER = {"GOL": 0, "ZAG": 1, "LAT": 2, "MEI": 3, "ATA": 4, "TEC": 5}


def expected_slots_for_formation(formation: str):
    """
    Retorna lista (slot_type, slot_index) na ordem:
    GOL1, ZAGs, LATs, MEIs, ATAs, TEC1
    """
    if formation not in FORMATION_MAP:
        raise ValidationError("Formação inválida.")

    counts = FORMATION_MAP[formation]
    out = [("GOL", 1)]

    for t in ["ZAG", "LAT", "MEI", "ATA"]:
        for i in range(1, counts[t] + 1):
            out.append((t, i))

    out.append(("TEC", 1))
    return out


def ensure_spots_for_lineup(lineup: Lineup):
    """
    Garante que existem exatamente os slots daquela formação.
    - Cria slots faltantes (player NULL).
    - Remove slots extras somente se estiverem vazios.
    """
    expected = set(expected_slots_for_formation(lineup.formation))

    qs = LineupSpot.objects.filter(lineup=lineup).select_related("player")
    existing = {(s.slot_type, s.slot_index): s for s in qs}

    to_create = []
    for (t, idx) in expected:
        if (t, idx) not in existing:
            to_create.append(LineupSpot(lineup=lineup, slot_type=t, slot_index=idx, player=None))
    if to_create:
        LineupSpot.objects.bulk_create(to_create)

    for key, spot in existing.items():
        if key not in expected:
            if spot.player_id is not None:
                raise ValidationError(
                    f"Não dá pra aplicar esta formação: existe jogador em um slot extra ({spot.slot_type}{spot.slot_index})."
                )
            spot.delete()


@login_required
def set_lineup(request, draft_id: int, team_id: int, week_number: int = None):
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    draft = get_object_or_404(Draft, id=draft_id)

    if week_number is not None:
        week, _ = Week.objects.get_or_create(
            draft=draft,
            number=week_number,
            defaults={"is_current": False},
        )
    else:
        week = Week.objects.filter(draft=draft, is_current=True).first()
        if not week:
            week, _ = Week.objects.get_or_create(
                draft=draft,
                number=1,
                defaults={"is_current": True},
            )

    if week.is_locked and not request.user.is_superuser:
        return HttpResponseForbidden("Esta Week está travada. Não é possível editar a escalação.")

    round_number = week.number

    lineup, _ = Lineup.objects.get_or_create(
        week=week,
        team=team,
        defaults={"formation": "433"},
    )

    preview_formation = request.GET.get("formation")
    selected_formation = preview_formation or lineup.formation
    if selected_formation not in FORMATION_MAP:
        selected_formation = lineup.formation

    roster_spots = (
        RosterSpot.objects
        .filter(draft=draft, team=team, dropped_at__isnull=True)
        .select_related("player")
        .order_by("player__position", "player__name")
    )
    roster_player_ids = [rs.player_id for rs in roster_spots]
    all_players = Player.objects.filter(id__in=roster_player_ids)

    gols = all_players.filter(position="GOL")
    zags = all_players.filter(position="ZAG")
    lats = all_players.filter(position="LAT")
    meis = all_players.filter(position="MEI")
    atas = all_players.filter(position="ATA")
    tecs = all_players.filter(position="TEC")

    if request.method == "POST":
        new_formation = request.POST.get("formation", lineup.formation)

        try:
            with transaction.atomic():
                if new_formation != lineup.formation:
                    current_players = (
                        LineupSpot.objects.filter(lineup=lineup, player__isnull=False)
                        .select_related("player")
                    )
                    for s in current_players:
                        if s.player and player_locked(s.player, round_number):
                            raise ValidationError(
                                "Não é possível mudar a formação: existe jogador travado na escalação (jogo já começou)."
                            )

                    if new_formation not in FORMATION_MAP:
                        raise ValidationError("Formação inválida.")

                    lineup.formation = new_formation
                    lineup.full_clean()
                    lineup.save(update_fields=["formation", "updated_at"])

                ensure_spots_for_lineup(lineup)

                limits = FORMATION_MAP.get(lineup.formation, {})
                for slot_type in ("ZAG", "LAT", "MEI", "ATA"):
                    allowed = limits.get(slot_type, 0)
                    LineupSpot.objects.filter(
                        lineup=lineup,
                        slot_type=slot_type,
                        slot_index__gt=allowed,
                    ).update(player=None)

                expected = expected_slots_for_formation(lineup.formation)

                spots = {
                    (s.slot_type, s.slot_index): s
                    for s in LineupSpot.objects.filter(lineup=lineup).select_related("player")
                }

                chosen_ids = []

                for (t, idx) in expected:
                    field = f"slot_{t}_{idx}"
                    pid = request.POST.get(field) or None
                    pid = int(pid) if pid else None

                    if not pid:
                        raise ValidationError("Preencha todos os slots antes de salvar.")

                    player = get_object_or_404(Player, id=pid)

                    if player.id not in roster_player_ids:
                        raise ValidationError(f"{player.name} não está no roster do seu time.")

                    if player.position != t:
                        raise ValidationError(
                            f"{player.name} é {player.position} e não pode ser escalado em {t}{idx}."
                        )

                    spot = spots[(t, idx)]
                    old_player = spot.player

                    if old_player and old_player.id != player.id and player_locked(old_player, round_number):
                        raise ValidationError(f"Você não pode remover/trocar {old_player.name}: jogo já começou.")

                    if (not old_player or old_player.id != player.id) and player_locked(player, round_number):
                        raise ValidationError(f"Você não pode escalar {player.name}: jogo já começou.")

                    chosen_ids.append(player.id)

                    spot.player = player
                    spot.full_clean()
                    spot.save(update_fields=["player", "set_at"])

                if len(chosen_ids) != len(set(chosen_ids)):
                    raise ValidationError("Você não pode repetir o mesmo jogador em mais de um slot.")

            messages.success(request, "Escalação salva!")

            matchup = Matchup.objects.filter(
                week=week
            ).filter(
                Q(home_team=team) | Q(away_team=team)
            ).first()

            if matchup:
                return redirect(
                    "matchup_detail",
                    draft_id=draft.id,
                    week_number=week.number,
                    matchup_id=matchup.id,
                )

            return redirect("current_week", draft_id=draft.id)

        except (ValidationError, Player.DoesNotExist) as e:
            messages.error(request, str(e))

    try:
        expected = expected_slots_for_formation(selected_formation)

        existing_qs = LineupSpot.objects.filter(lineup=lineup).select_related("player")
        existing = {(s.slot_type, s.slot_index): s for s in existing_qs}

        slots = []
        for (t, idx) in expected:
            s = existing.get((t, idx))
            if s:
                slots.append(s)
            else:
                slots.append(LineupSpot(lineup=lineup, slot_type=t, slot_index=idx, player=None))

    except ValidationError as e:
        messages.error(request, str(e))
        slots = list(LineupSpot.objects.filter(lineup=lineup).select_related("player"))

    slots.sort(key=lambda s: (SLOT_ORDER.get(s.slot_type, 99), s.slot_index or 999))

    locked_player_ids = set()
    for s in LineupSpot.objects.filter(lineup=lineup).select_related("player"):
        if s.player_id and player_locked(s.player, round_number):
            locked_player_ids.add(s.player_id)

    formation_choices = Lineup._meta.get_field("formation").choices

    return render(request, "league/set_lineup.html", {
        "draft": draft,
        "team": team,
        "week": week,
        "active_tab": "lineup",
        "lineup": lineup,
        "formation_choices": formation_choices,
        "selected_formation": selected_formation,
        "slots": slots,
        "locked_player_ids": locked_player_ids,
        "gols": gols,
        "zags": zags,
        "lats": lats,
        "meis": meis,
        "atas": atas,
        "tecs": tecs,
        "my_matchup": get_team_matchup_for_week(week, team),
    })
# ============================================================
# Week controls
# ============================================================
@login_required
@require_POST
def postpone_current_week(request, draft_id: int):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Apenas o admin pode adiar a rodada.")

    draft = get_object_or_404(Draft, id=draft_id)

    current_week = Week.objects.filter(draft=draft, is_current=True).first()
    if not current_week:
        return redirect("draft_board", draft_id=draft.id)

    current_week.is_postponed = True
    current_week.save(update_fields=["is_postponed"])

    return redirect("draft_board", draft_id=draft.id)


# ============================================================
# Week view
# ============================================================
@login_required
def current_week_view(request, draft_id: int):
    draft = get_object_or_404(Draft, id=draft_id)

    week = Week.objects.filter(draft=draft, is_current=True).first()
    if not week:
        return render(request, "league/current_week.html", {
            "draft": draft,
            "week": None,
            "current_week": None,
            "matchups": [],
            "team": None,
            "active_tab": "current_week",
            "error": "Nenhuma Week marcada como atual (is_current=True).",
        })

    matchups = (
        Matchup.objects
        .filter(week=week)
        .select_related("home_team", "away_team", "week")
        .order_by("id")
    )

    return render(request, "league/current_week.html", {
        "draft": draft,
        "week": week,
        "current_week": week,
        "matchups": matchups,
        "team": None,
        "active_tab": "current_week",
        "error": None,
    })


# ============================================================
# Admin edit matchup scores (manual override)
# ============================================================
@login_required
@transaction.atomic
def edit_week_scores(request, draft_id: int, week_number: int):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Apenas o admin pode editar pontuações.")

    draft = get_object_or_404(Draft, id=draft_id)
    week, _ = Week.objects.get_or_create(draft=draft, number=week_number)

    matchups = list(
        Matchup.objects.filter(week=week).select_related("home_team", "away_team").order_by("id")
    )

    if request.method == "POST":
        for m in matchups:
            hs = request.POST.get(f"home_score_{m.id}", "").strip()
            a_s = request.POST.get(f"away_score_{m.id}", "").strip()

            try:
                m.home_score = float(hs) if hs != "" else 0
                m.away_score = float(a_s) if a_s != "" else 0
                m.save(update_fields=["home_score", "away_score"])
            except ValueError:
                messages.error(request, "Pontuação inválida. Use números (ex: 62.5).")
                return redirect("edit_week_scores", draft_id=draft.id, week_number=week_number)

        messages.success(request, f"Pontuações da Week {week_number} salvas!")
        return redirect("current_week", draft_id=draft.id)

    return render(request, "league/edit_week_scores.html", {
        "draft": draft,
        "week": week,
        "matchups": matchups,
        "team": None,
        "active_tab": "current_week",
    })


def get_team_matchup_for_week(week, team):
    if not week or not team:
        return None

    return (
        Matchup.objects
        .filter(week=week)
        .filter(Q(home_team=team) | Q(away_team=team))
        .select_related("home_team", "away_team", "week")
        .first()
    )


def lineup_display_data(week, team):
    """
    Retorna (lineup, spots_ordenados, total_points, points_map)
    """
    lineup, _ = Lineup.objects.get_or_create(
        week=week,
        team=team,
        defaults={"formation": "433"},
    )

    try:
        ensure_spots_for_lineup(lineup)
    except ValidationError:
        pass

    spots = list(
        LineupSpot.objects
        .filter(lineup=lineup)
        .select_related("player")
    )
    spots.sort(key=lambda s: (SLOT_ORDER.get(s.slot_type, 99), s.slot_index or 999))

    player_ids = [s.player_id for s in spots if s.player_id]
    score_qs = PlayerWeekScore.objects.filter(week=week, player_id__in=player_ids)
    points_map = {ps.player_id: ps.points for ps in score_qs}

    total = 0
    for s in spots:
        if s.player_id:
            total += float(points_map.get(s.player_id, 0))

    return lineup, spots, total, points_map


@login_required
def matchup_detail(request, draft_id: int, week_number: int, matchup_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    week = get_object_or_404(Week, draft=draft, number=week_number)

    matchup = (
        Matchup.objects
        .filter(id=matchup_id, week=week)
        .select_related("home_team", "away_team", "week")
        .first()
    )

    if not matchup:
        messages.error(request, "Matchup não encontrado.")
        return redirect("current_week", draft_id=draft.id)

    home_lineup, home_spots, home_total, home_points_map = lineup_display_data(
        week, matchup.home_team
    )
    away_lineup, away_spots, away_total, away_points_map = lineup_display_data(
        week, matchup.away_team
    )

    team = None
    if matchup.home_team.user_id == request.user.id:
        team = matchup.home_team
    elif matchup.away_team.user_id == request.user.id:
        team = matchup.away_team

    my_matchup = get_team_matchup_for_week(week, team) if team else None

    return render(request, "league/matchup.html", {
        "draft": draft,
        "week": week,
        "team": team,
        "active_tab": "matchups",
        "matchup": matchup,
        "home_team": matchup.home_team,
        "away_team": matchup.away_team,
        "home_lineup": home_lineup,
        "away_lineup": away_lineup,
        "home_spots": home_spots,
        "away_spots": away_spots,
        "home_total": home_total,
        "away_total": away_total,
        "home_points_map": home_points_map,
        "away_points_map": away_points_map,
        "my_matchup": my_matchup,
    })

def build_standings(draft):
    teams = list(Team.objects.filter(league=draft.league).order_by("id"))

    table = {
        team.id: {
            "team": team,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "points_for": 0.0,
            "points_against": 0.0,
            "games": 0,
        }
        for team in teams
    }

    matchups = (
        Matchup.objects
        .filter(week__draft=draft)
        .select_related("home_team", "away_team", "week")
        .order_by("week__number", "id")
    )

    for m in matchups:
        if m.home_team_id not in table or m.away_team_id not in table:
            continue

        home = table[m.home_team_id]
        away = table[m.away_team_id]

        home_score = float(m.home_score or 0)
        away_score = float(m.away_score or 0)

        home["games"] += 1
        away["games"] += 1

        home["points_for"] += home_score
        home["points_against"] += away_score

        away["points_for"] += away_score
        away["points_against"] += home_score

        if home_score > away_score:
            home["wins"] += 1
            away["losses"] += 1
        elif away_score > home_score:
            away["wins"] += 1
            home["losses"] += 1
        else:
            home["ties"] += 1
            away["ties"] += 1

    standings = list(table.values())
    standings.sort(
        key=lambda row: (
            -row["wins"],
            -row["points_for"],
            row["points_against"],
            row["team"].name.lower() if row["team"].name else "",
        )
    )

    for idx, row in enumerate(standings, start=1):
        row["rank"] = idx

    return standings

from .models import Draft, Team, Week, TeamBudget

@login_required
def standings_view(request, draft_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    week = Week.objects.filter(draft=draft, is_current=True).first()

    team = None
    if request.user.is_superuser:
        team = Team.objects.filter(league=draft.league).order_by("id").first()
    else:
        team = Team.objects.filter(league=draft.league, user_id=request.user.id).first()

    my_matchup = get_team_matchup_for_week(week, team) if week and team else None
    standings = build_standings(draft)

    team_ids = [row["team"].id if isinstance(row, dict) else row.team.id for row in standings]

    budget_map = {
        tb.team_id: tb.faab_balance
        for tb in TeamBudget.objects.filter(team_id__in=team_ids)
    }

    for row in standings:
        row_team = row["team"] if isinstance(row, dict) else row.team
        current_faab = budget_map.get(row_team.id)

        if current_faab is None:
            current_faab = row_team.budget

        if isinstance(row, dict):
            row["faab_balance"] = current_faab
        else:
            row.faab_balance = current_faab

    return render(request, "league/standings.html", {
        "draft": draft,
        "week": week,
        "team": team,
        "my_matchup": my_matchup,
        "standings": standings,
        "active_tab": "standings",
    })

@login_required
def propose_trade_player(request, draft_id: int, target_team_id: int, target_player_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    target_team = get_object_or_404(Team, id=target_team_id)
    target_player = get_object_or_404(Player, id=target_player_id)

    my_team = Team.objects.filter(
        league=draft.league,
        user_id=request.user.id,
    ).first()

    if not my_team:
        return HttpResponseForbidden("Você não possui um time nesta liga.")

    if my_team.id == target_team.id:
        return HttpResponseForbidden("Você não pode propor troca para o próprio time.")

    my_players = (
        RosterSpot.objects
        .filter(
            team=my_team,
            draft=draft,
            dropped_at__isnull=True,
        )
        .select_related("player")
        .order_by("player__position", "player__name")
    )

    target_players = (
        RosterSpot.objects
        .filter(
            team=target_team,
            draft=draft,
            dropped_at__isnull=True,
        )
        .select_related("player")
        .order_by("player__position", "player__name")
    )

    if request.method == "POST":
        my_selected = request.POST.getlist("my_players")
        target_selected = request.POST.getlist("target_players")

        if not my_selected:
            messages.error(request, "Selecione pelo menos um jogador seu para oferecer.")
            return render(request, "league/propose_trade_player.html", {
                "draft": draft,
                "team": my_team,
                "my_team": my_team,
                "target_team": target_team,
                "target_player": target_player,
                "my_players": my_players,
                "target_players": target_players,
                "active_tab": "roster",
            })

        if not target_selected:
            messages.error(request, "Selecione pelo menos um jogador do outro time para receber.")
            return render(request, "league/propose_trade_player.html", {
                "draft": draft,
                "team": my_team,
                "my_team": my_team,
                "target_team": target_team,
                "target_player": target_player,
                "my_players": my_players,
                "target_players": target_players,
                "active_tab": "roster",
            })

        trade = TradeProposal.objects.create(
            draft=draft,
            from_team=my_team,
            to_team=target_team,
        )

        # jogadores que EU envio
        for pid in my_selected:
            TradeItem.objects.create(
                trade=trade,
                player_id=pid,
                from_team=my_team,
            )

        # jogadores que EU recebo
        for pid in target_selected:
            TradeItem.objects.create(
                trade=trade,
                player_id=pid,
                from_team=target_team,
            )

        Notification.objects.create(
            draft=draft,
            team=target_team,
            type="trade_received",
            message=f"Nova proposta de trade recebida de {my_team.name}.",
            trade=trade,
        )

        messages.success(request, "Proposta de trade enviada com sucesso.")
        return redirect("team_roster", draft_id=draft.id, team_id=target_team.id)

    return render(request, "league/propose_trade_player.html", {
        "draft": draft,
        "team": my_team,
        "my_team": my_team,
        "target_team": target_team,
        "target_player": target_player,
        "my_players": my_players,
        "target_players": target_players,
        "active_tab": "roster",
    })

@login_required
def notifications_view(request, draft_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    week = Week.objects.filter(draft=draft, is_current=True).first()

    team = Team.objects.filter(league=draft.league, user=request.user).first()
    if not team:
        return HttpResponseForbidden("Você não possui um time vinculado nesta liga.")

    my_matchup = get_team_matchup_for_week(week, team) if week and team else None

    notifications = Notification.objects.filter(
        draft=draft,
        team=team,
    ).select_related("trade", "waiver_claim")[:100]

    Notification.objects.filter(
        draft=draft,
        team=team,
        is_read=False,
    ).update(is_read=True)

    return render(request, "league/notifications.html", {
        "draft": draft,
        "week": week,
        "team": team,
        "my_matchup": my_matchup,
        "notifications": notifications,
        "active_tab": "notifications",
    })
@login_required
@require_POST
@transaction.atomic
def accept_trade(request, trade_id: int):
    trade = get_object_or_404(
        TradeProposal.objects.select_related("draft", "from_team", "to_team"),
        id=trade_id,
    )

    my_team = Team.objects.filter(
        league=trade.draft.league,
        user=request.user,
    ).first()

    if not my_team or my_team.id != trade.to_team_id:
        return HttpResponseForbidden("Você não pode aceitar esta trade.")

    if trade.status != "pending":
        messages.error(request, "Essa proposta já foi respondida.")
        return redirect("notifications_view", draft_id=trade.draft.id)

    sent_items = list(trade.items.filter(from_team=trade.from_team).select_related("player"))
    received_items = list(trade.items.filter(from_team=trade.to_team).select_related("player"))

    # valida se todos ainda estão nos times corretos
    for item in sent_items:
        exists = RosterSpot.objects.filter(
            draft=trade.draft,
            team=trade.from_team,
            player=item.player,
            dropped_at__isnull=True,
        ).exists()
        if not exists:
            messages.error(request, f"{item.player.name} não está mais no roster de origem.")
            trade.status = "rejected"
            trade.responded_at = timezone.now()
            trade.save(update_fields=["status", "responded_at"])
            return redirect("notifications_view", draft_id=trade.draft.id)

    for item in received_items:
        exists = RosterSpot.objects.filter(
            draft=trade.draft,
            team=trade.to_team,
            player=item.player,
            dropped_at__isnull=True,
        ).exists()
        if not exists:
            messages.error(request, f"{item.player.name} não está mais no roster de origem.")
            trade.status = "rejected"
            trade.responded_at = timezone.now()
            trade.save(update_fields=["status", "responded_at"])
            return redirect("notifications_view", draft_id=trade.draft.id)

    # executa a troca
    for item in sent_items:
        spot = RosterSpot.objects.get(
            draft=trade.draft,
            team=trade.from_team,
            player=item.player,
            dropped_at__isnull=True,
        )
        spot.team = trade.to_team
        spot.acquired_via = "TRADE"
        spot.acquired_at = timezone.now()
        spot.save(update_fields=["team", "acquired_via", "acquired_at"])

        Transaction.objects.create(
            draft=trade.draft,
            team=trade.to_team,
            player=item.player,
            type="TRADE",
            notes=f"Recebido em trade de {trade.from_team.name}",
        )

    for item in received_items:
        spot = RosterSpot.objects.get(
            draft=trade.draft,
            team=trade.to_team,
            player=item.player,
            dropped_at__isnull=True,
        )
        spot.team = trade.from_team
        spot.acquired_via = "TRADE"
        spot.acquired_at = timezone.now()
        spot.save(update_fields=["team", "acquired_via", "acquired_at"])

        Transaction.objects.create(
            draft=trade.draft,
            team=trade.from_team,
            player=item.player,
            type="TRADE",
            notes=f"Recebido em trade de {trade.to_team.name}",
        )

    trade.status = "accepted"
    trade.responded_at = timezone.now()
    trade.save(update_fields=["status", "responded_at"])

    Notification.objects.create(
        draft=trade.draft,
        team=trade.from_team,
        type="trade_accepted",
        message=f"Sua proposta para {trade.to_team.name} foi aceita.",
        trade=trade,
    )

    Notification.objects.create(
        draft=trade.draft,
        team=trade.to_team,
        type="trade_accepted",
        message=f"Você aceitou a proposta de {trade.from_team.name}.",
        trade=trade,
    )

    messages.success(request, "Trade aceita com sucesso.")
    return redirect("notifications_view", draft_id=trade.draft.id)
@login_required
@require_POST
def reject_trade(request, trade_id: int):
    trade = get_object_or_404(
        TradeProposal.objects.select_related("draft", "from_team", "to_team"),
        id=trade_id,
    )

    my_team = Team.objects.filter(
        league=trade.draft.league,
        user=request.user,
    ).first()

    if not my_team or my_team.id != trade.to_team_id:
        return HttpResponseForbidden("Você não pode rejeitar esta trade.")

    if trade.status != "pending":
        messages.error(request, "Essa proposta já foi respondida.")
        return redirect("notifications_view", draft_id=trade.draft.id)

    trade.status = "rejected"
    trade.responded_at = timezone.now()
    trade.save(update_fields=["status", "responded_at"])

    Notification.objects.create(
        draft=trade.draft,
        team=trade.from_team,
        type="trade_rejected",
        message=f"Sua proposta para {trade.to_team.name} foi rejeitada.",
        trade=trade,
    )

    Notification.objects.create(
        draft=trade.draft,
        team=trade.to_team,
        type="trade_rejected",
        message=f"Você rejeitou a proposta de {trade.from_team.name}.",
        trade=trade,
    )

    messages.success(request, "Trade rejeitada.")
    return redirect("notifications_view", draft_id=trade.draft.id)

@login_required
def player_detail(request, player_id: int):
    player = get_object_or_404(Player, id=player_id)

    draft = Draft.objects.order_by("-id").first()

    scores = PlayerWeekScore.objects.filter(player=player)

    if draft:
        scores = scores.filter(week__draft=draft)

    scores = scores.select_related("week").order_by("week__number")

    stats = scores.aggregate(
        total=Sum("points"),
        avg=Avg("points"),
    )

    chart_labels = [f"R{score.week.number}" for score in scores]
    chart_points = [float(score.points) for score in scores]

    return render(request, "league/player_detail.html", {
        "player": player,
        "draft": draft,
        "scores": scores,
        "stats": stats,
        "chart_labels": chart_labels,
        "chart_points": chart_points,
    })

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST


@login_required
@require_POST
def reorder_roster(request, draft_id: int, team_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    viewed_team = get_object_or_404(Team, id=team_id)

    my_team = Team.objects.filter(
        league=draft.league,
        user=request.user,
    ).first()

    if not my_team or my_team.id != viewed_team.id:
        return HttpResponseForbidden("Você não pode reorganizar este roster.")

    try:
        payload = json.loads(request.body)
        spot_ids = payload.get("spot_ids", [])
    except Exception:
        return JsonResponse({"ok": False, "error": "JSON inválido."}, status=400)

    current_spot_ids = list(
        RosterSpot.objects
        .filter(draft=draft, team=viewed_team, dropped_at__isnull=True)
        .values_list("id", flat=True)
    )

    if sorted(spot_ids) != sorted(current_spot_ids):
        return JsonResponse({"ok": False, "error": "Lista inválida."}, status=400)

    spots = {
        spot.id: spot
        for spot in RosterSpot.objects.filter(id__in=spot_ids)
    }

    to_update = []
    for index, spot_id in enumerate(spot_ids):
        spot = spots[spot_id]
        spot.manual_order = index
        to_update.append(spot)

    RosterSpot.objects.bulk_update(to_update, ["manual_order"])

    return JsonResponse({"ok": True})

    from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from .models import Draft, Week, Team, Matchup, TeamBudget, RosterSpot

def get_team_matchup_for_week(week, team):
    if not week or not team:
        return None

    return Matchup.objects.filter(week=week).filter(
        models.Q(home_team=team) | models.Q(away_team=team)
    ).first()


@login_required
def dashboard(request):
    draft = Draft.objects.order_by("-id").first()
    if not draft:
        return render(request, "league/dashboard.html", {
            "draft": None,
            "week": None,
            "team": None,
            "my_matchup": None,
            "faab_balance": 0,
            "roster_count": 0,
            "active_tab": "dashboard",
        })

    week = Week.objects.filter(draft=draft, is_current=True).first()

    team = Team.objects.filter(
        league=draft.league,
        user=request.user,
    ).first()

    my_matchup = get_team_matchup_for_week(week, team) if week and team else None

    faab_balance = 0
    if team:
        budget, _ = TeamBudget.objects.get_or_create(team=team)
        faab_balance = budget.faab_balance

    roster_count = 0
    if team:
        roster_count = (
            RosterSpot.objects
            .filter(draft=draft, team=team, dropped_at__isnull=True)
            .count()
        )

    return render(request, "league/dashboard.html", {
        "draft": draft,
        "week": week,
        "team": team,
        "my_matchup": my_matchup,
        "faab_balance": faab_balance,
        "roster_count": roster_count,
        "active_tab": "dashboard",
    })

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect

@login_required
def delete_notification(request, notification_id: int):
    notification = get_object_or_404(
        Notification,
        id=notification_id,
        team__user=request.user,
    )

    if request.method == "POST":
        notification.delete()

    return redirect(request.META.get("HTTP_REFERER", "/"))

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect

@login_required
def delete_notification(request, notification_id: int):
    notification = get_object_or_404(
        Notification,
        id=notification_id,
        team__user=request.user,
    )

    if request.method == "POST":
        if not (notification.trade and notification.type == "trade_received" and notification.trade.status == "pending"):
            notification.delete()

    return redirect(request.META.get("HTTP_REFERER", "/"))


from django.utils import timezone
from datetime import timedelta

@login_required
def clear_old_notifications(request):
    if request.method == "POST":
        limit_date = timezone.now() - timedelta(days=7)

        Notification.objects.filter(
            team__user=request.user,
            created_at__lt=limit_date,
        ).exclude(
            trade__status="pending",
            type="trade_received",
        ).delete()

    return redirect(request.META.get("HTTP_REFERER", "/"))


@login_required
def clear_all_notifications(request):
    if request.method == "POST":
        Notification.objects.filter(team__user=request.user).exclude(
            trade__status="pending",
            type="trade_received",
        ).delete()

    return redirect(request.META.get("HTTP_REFERER", "/"))