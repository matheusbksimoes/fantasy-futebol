from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.utils import timezone
from django.db import transaction
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.core.exceptions import ValidationError

from .models import (
    Draft, DraftPick, Team, Player, RosterSpot, Transaction,
    Week, LineupSpot, Matchup
)

# Lineup V2 (formação + slots)
from league.models import Lineup  # se Lineup estiver em .models, troque para: from .models import Lineup
from league.services.lineup_service import ensure_slots_for_formation
from league.services.lock_service import player_locked


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
        "week": current_week,          # ✅ pro base.html
        "team": None,                  # ✅ base.html não quebra
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

    # Permissão
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


@login_required
def team_roster(request, draft_id: int, team_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    team = get_object_or_404(Team, id=team_id)
    week = Week.objects.filter(draft=draft, is_current=True).first()

    roster_spots = (
        RosterSpot.objects
        .select_related("player")
        .filter(draft=draft, team=team, dropped_at__isnull=True)
        .order_by("player__position", "player__name")
    )

    return render(request, "league/team_roster.html", {
        "team": team,
        "draft": draft,
        "week": week,
        "roster_spots": roster_spots,
        "active_tab": "roster",
    })



@login_required
@require_POST
def drop_player(request, team_id: int, player_id: int):
    team = get_object_or_404(Team, id=team_id)
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

    draft = Draft.objects.order_by("-id").first()
    if not draft:
        return render(request, "league/free_agents.html", {
            "team": team,
            "draft": None,
            "week": None,
            "free_agents": [],
            "active_tab": "free_agents",
        })

    week = Week.objects.filter(draft=draft, is_current=True).first()

    active_player_ids = (
        RosterSpot.objects
        .filter(dropped_at__isnull=True)
        .values_list("player_id", flat=True)
    )
    free_agents = Player.objects.exclude(id__in=active_player_ids).order_by("name")

    return render(request, "league/free_agents.html", {
        "team": team,
        "draft": draft,
        "week": week,
        "free_agents": free_agents,
        "active_tab": "free_agents",
    })



@login_required
@require_POST
def add_free_agent(request, team_id: int, player_id: int):
    team = get_object_or_404(Team, id=team_id)
    player = get_object_or_404(Player, id=player_id)

    draft = Draft.objects.order_by("-id").first()
    if not draft:
        messages.error(request, "Nenhum draft encontrado.")
        return redirect("free_agents_list", team_id=team.id)

    active_spot = RosterSpot.objects.filter(
        draft=draft,
        player=player,
        dropped_at__isnull=True
    ).first()

    if active_spot:
        messages.error(request, f"{player.name} já pertence a um time.")
        return redirect("free_agents_list", team_id=team.id)

    spot = RosterSpot.objects.filter(draft=draft, player=player).first()

    if spot:
        spot.team = team
        spot.acquired_via = "FA"
        spot.acquired_at = timezone.now()
        spot.dropped_at = None
        spot.save()
    else:
        RosterSpot.objects.create(
            draft=draft,
            team=team,
            player=player,
            acquired_via="FA",
        )

    messages.success(request, f"{player.name} foi adicionado ao {team.name} (Free Agency).")
    return redirect("team_roster", draft_id=draft.id, team_id=team.id)


@login_required
def team_roster_legacy(request, team_id: int):
    draft = Draft.objects.order_by("-id").first()
    if not draft:
        return HttpResponseForbidden("Nenhum draft encontrado.")
    return redirect("team_roster", draft_id=draft.id, team_id=team_id)


@login_required
def transactions_list(request, draft_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    week = Week.objects.filter(draft=draft, is_current=True).first()

    transactions = (
        Transaction.objects
        .filter(draft=draft)
        .select_related("team", "player")
        .order_by("-created_at")[:200]
    )

    return render(request, "league/transactions.html", {
        "draft": draft,
        "week": week,
        "team": None,
        "transactions": transactions,
        "active_tab": "transactions",
    })




# ✅ ESCALAÇÃO (ÚNICA) — V2 com Formação + Slots + Lock
@login_required
def set_lineup(request, draft_id: int, team_id: int):
    team = get_object_or_404(Team, id=team_id)
    draft = get_object_or_404(Draft, id=draft_id)

    # week atual
    week = Week.objects.filter(draft=draft, is_current=True).first()
    if not week:
        week, _ = Week.objects.get_or_create(
            draft=draft,
            number=1,
            defaults={"is_current": True},
        )

    round_number = week.number

    lineup, _ = Lineup.objects.get_or_create(
        fantasy_team=team,
        round_number=round_number,
        defaults={"formation": "433"},
    )

    try:
        ensure_slots_for_formation(lineup)
    except ValidationError as e:
        messages.error(request, str(e))
        return redirect("set_lineup", draft_id=draft.id, team_id=team.id)

    # roster ativo do time
    roster_spots = (
        RosterSpot.objects
        .filter(draft=draft, team=team, dropped_at__isnull=True)
        .select_related("player")
        .order_by("player__position", "player__name")
    )
    all_players = Player.objects.filter(id__in=[rs.player_id for rs in roster_spots])

    if request.method == "POST":
        new_formation = request.POST.get("formation", lineup.formation)

        try:
            with transaction.atomic():
                if new_formation != lineup.formation:
                    lineup.formation = new_formation
                    lineup.full_clean()
                    lineup.save()
                    ensure_slots_for_formation(lineup)

                slots = list(
                    lineup.slots.select_related("player").order_by("slot_type", "slot_index")
                )

                chosen_ids = []
                slot_updates = []

                for s in slots:
                    field_name = f"slot_{s.slot_type}_{s.slot_index}"
                    pid = request.POST.get(field_name) or None
                    pid = int(pid) if pid else None

                    old_player = s.player
                    new_player = Player.objects.get(id=pid) if pid else None

                    if old_player and old_player != new_player and player_locked(old_player, round_number):
                        raise ValidationError(
                            f"Você não pode remover/trocar {old_player.name}: jogo já começou."
                        )

                    if new_player and old_player != new_player and player_locked(new_player, round_number):
                        raise ValidationError(
                            f"Você não pode escalar {new_player.name}: jogo já começou."
                        )

                    if new_player and new_player.position != s.slot_type:
                        raise ValidationError(
                            f"{new_player.name} é {new_player.position} e não pode ser escalado em {s.slot_type}{s.slot_index}."
                        )

                    if new_player:
                        chosen_ids.append(new_player.id)

                    slot_updates.append((s, new_player))

                if len(chosen_ids) != len(set(chosen_ids)):
                    raise ValidationError("Você não pode repetir o mesmo jogador em mais de um slot.")

                for s, p in slot_updates:
                    if p is None:
                        raise ValidationError(
                            "Preencha todos os slots antes de salvar (GOL, ZAG, LAT, MEI, ATA e TEC)."
                        )

                for s, p in slot_updates:
                    s.player = p
                    s.save()

            messages.success(request, "Escalação salva!")
            return redirect("set_lineup", draft_id=draft.id, team_id=team.id)

        except (ValidationError, Player.DoesNotExist) as e:
            messages.error(request, str(e))

    gols = all_players.filter(position="GOL")
    zags = all_players.filter(position="ZAG")
    lats = all_players.filter(position="LAT")
    meis = all_players.filter(position="MEI")
    atas = all_players.filter(position="ATA")
    tecs = all_players.filter(position="TEC")

    slots = lineup.slots.select_related("player").order_by("slot_type", "slot_index")

    locked_player_ids = set()
    for s in slots:
        if s.player and player_locked(s.player, round_number):
            locked_player_ids.add(s.player.id)

    formation_choices = Lineup._meta.get_field("formation").choices

    return render(request, "league/set_lineup.html", {
    "draft": draft,
    "team": team,
    "week": week,

    "active_tab": "lineup",  # ✅ ADICIONE ISSO

    "lineup": lineup,
    "formation_choices": formation_choices,
    "slots": slots,
    "gols": gols,
    "zags": zags,
    "lats": lats,
    "meis": meis,
    "atas": atas,
    "tecs": tecs,
    "locked_player_ids": locked_player_ids,
})


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


@login_required
def current_week_matchups(request, draft_id: int):
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

    matchups = Matchup.objects.filter(week=week).select_related("home_team", "away_team")

    return render(request, "league/current_week.html", {
        "draft": draft,
        "week": week,
        "current_week": week,
        "matchups": matchups,
        "team": None,
        "active_tab": "current_week",
        "error": None,
    })



@login_required
def current_week_view(request, draft_id: int):
    draft = get_object_or_404(Draft, id=draft_id)

    current_week = Week.objects.filter(draft=draft, is_current=True).first()
    if not current_week:
        return render(request, "league/current_week.html", {
            "draft": draft,
            "current_week": None,
            "matchups": [],
            "error": "Nenhuma Week marcada como atual (is_current=True).",
            "team": my_team,
            "week": current_week,   # <- pro menu mostrar Week
            "active_tab": "week",
        })

    matchups = (
        Matchup.objects
        .filter(week=current_week)
        .select_related("home_team", "away_team", "week")
        .order_by("id")
    )

    return render(request, "league/current_week.html", {
        "draft": draft,
        "current_week": current_week,
        "matchups": matchups,
        "error": None,
    })
