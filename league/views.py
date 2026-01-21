from django.shortcuts import render, get_object_or_404
from .models import Draft, Team, DraftPick
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.utils import timezone
from django.db import transaction


from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404

from .models import Draft, DraftPick, Team
from .models import Draft, DraftPick, Player, RosterSpot


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

    # pick atual
    current_pick = (
        DraftPick.objects
        .filter(draft=draft, is_current=True)
        .select_related("team", "player")
        .first()
    )

    # ✅ permissões pro botão Draftar
    can_draft = False
    if current_pick:
        if request.user.is_superuser:
            can_draft = True
        elif current_pick.team.user_id == request.user.id:
            can_draft = True

    # ✅ montar matriz (round x teams) usando round_number e team
    rounds = draft.rounds
    board_rows = []
    picks = list(picks_qs)

    # index rápido: (round_number, team_id) -> pick
    pick_map = {(p.round_number, p.team_id): p for p in picks}

    for r in range(1, rounds + 1):
        row = []
        for t in teams:
            row.append(pick_map.get((r, t.id)))
        board_rows.append(row)

    return render(request, "league/draft_board.html", {
        "draft": draft,
        "teams": teams,
        "board_rows": board_rows,
        "current_pick": current_pick,
        "can_draft": can_draft,
    })

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
            "positions": Player.POSITION_CHOICES,
            "teams_filter": [],
            "selected_position": "",
            "selected_real_team": "",
        })

    # Permissão: superuser OU dono do time do pick
    if not request.user.is_superuser:
        if current_pick.team.user_id is None or current_pick.team.user_id != request.user.id:
            return HttpResponseForbidden(
                "Você não está na vez (ou o time ainda não está vinculado a um usuário)."
            )

    # filtros (opcionais) vindos por GET
    selected_position = request.GET.get("position", "").strip()
    selected_real_team = request.GET.get("real_team", "").strip()

    # jogadores já draftados neste draft
    drafted_ids = DraftPick.objects.filter(
        draft=draft, player__isnull=False
    ).values_list("player_id", flat=True)

    qs = Player.objects.exclude(id__in=drafted_ids).order_by("name")

    if selected_position:
        qs = qs.filter(position=selected_position)

    if selected_real_team:
        qs = qs.filter(real_team=selected_real_team)

    available_players = list(qs[:500])  # evita ficar gigante na tela

    # lista de times reais pra dropdown
    teams_filter = (
        Player.objects.order_by("real_team")
        .values_list("real_team", flat=True)
        .distinct()
    )

    if request.method == "POST":
        player_id = request.POST.get("player_id")
        if not player_id:
            return render(request, "league/make_pick.html", {
                "draft": draft,
                "current_pick": current_pick,
                "available_players": available_players,
                "error": "Selecione um jogador.",
                "positions": Player.POSITION_CHOICES,
                "teams_filter": teams_filter,
                "selected_position": selected_position,
                "selected_real_team": selected_real_team,
            })

        player = get_object_or_404(Player, id=player_id)

        # segurança: não deixar draftar jogador já escolhido
        already_taken = DraftPick.objects.filter(
            draft=draft, player=player
        ).exists()
        if already_taken:
            return render(request, "league/make_pick.html", {
                "draft": draft,
                "current_pick": current_pick,
                "available_players": available_players,
                "error": "Esse jogador já foi draftado.",
                "positions": Player.POSITION_CHOICES,
                "teams_filter": teams_filter,
                "selected_position": selected_position,
                "selected_real_team": selected_real_team,
            })

        # 1) salva o pick
        current_pick.player = player
        current_pick.is_current = False
        current_pick.save()

        # 2) cria roster spot
        RosterSpot.objects.create(
            draft=draft,
            team=current_pick.team,
            player=player,
            acquired_via="DRAFT",
        )

        # 3) avança para o próximo pick
        next_pick = (
            DraftPick.objects
            .filter(draft=draft, overall_number__gt=current_pick.overall_number)
            .order_by("overall_number")
            .first()
        )
        if next_pick:
            next_pick.is_current = True
            next_pick.save()

        return redirect("draft_board", draft_id=draft.id)

    return render(request, "league/make_pick.html", {
        "draft": draft,
        "current_pick": current_pick,
        "available_players": available_players,
        "error": "",
        "positions": Player.POSITION_CHOICES,
        "teams_filter": teams_filter,
        "selected_position": selected_position,
        "selected_real_team": selected_real_team,
    })
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.utils import timezone
from django.db import transaction

from .models import Draft, DraftPick, Team, Player


@login_required
@transaction.atomic
def start_draft_view(request, draft_id: int):
    # só superuser inicia (por enquanto)
    if not request.user.is_superuser:
        return HttpResponseForbidden("Apenas o admin pode iniciar o draft.")

    draft = get_object_or_404(Draft, id=draft_id)

    # limpa current
    DraftPick.objects.filter(draft=draft).update(is_current=False)

    # pega o primeiro pick ainda vazio
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
            "error": "Não há pick atual (draft não iniciado ou já terminou)."
        })

    # 🔐 Permissão
    if not request.user.is_superuser:
        if current_pick.team.user_id is None or current_pick.team.user_id != request.user.id:
            return HttpResponseForbidden(
                "Você não está na vez (ou o time ainda não está vinculado a um usuário)."
            )

    # 🔎 filtros (GET)
    query = request.GET.get("q", "").strip()
    pos = request.GET.get("pos", "").strip()
    team = request.GET.get("team", "").strip()

    # Jogadores já draftados
    drafted_ids = (
        DraftPick.objects
        .filter(draft=draft, player__isnull=False)
        .values_list("player_id", flat=True)
    )

    # Base: jogadores disponíveis
    base_qs = Player.objects.exclude(id__in=drafted_ids)

    # Lista de times para o dropdown (somente dos disponíveis)
    teams = (
        base_qs.exclude(real_team__isnull=True)
        .exclude(real_team__exact="")
        .values_list("real_team", flat=True)
        .distinct()
        .order_by("real_team")
    )

    # Aplica filtros
    available_players = base_qs

    if pos in {"GOL", "LAT", "ZAG", "MEI", "ATA", "TEC"}:
        available_players = available_players.filter(position=pos)

    if team:
        available_players = available_players.filter(real_team=team)

    if query:
        available_players = available_players.filter(name__icontains=query)

    available_players = available_players.order_by("name")

    # 📥 POST → confirmar pick
    if request.method == "POST":
        player_id = request.POST.get("player_id")

        if not player_id:
            return render(request, "league/make_pick.html", {
                "draft": draft,
                "current_pick": current_pick,
                "available_players": available_players,
                "teams": teams,
                "q": query,
                "pos": pos,
                "team": team,
                "error": "Selecione um jogador.",
            })

        player = get_object_or_404(Player, id=player_id)

        if DraftPick.objects.filter(draft=draft, player=player).exists():
            return render(request, "league/make_pick.html", {
                "draft": draft,
                "current_pick": current_pick,
                "available_players": available_players,
                "teams": teams,
                "q": query,
                "pos": pos,
                "team": team,
                "error": "Esse jogador já foi draftado.",
            })

        current_pick.player = player
        current_pick.made_at = timezone.now()
        current_pick.is_current = False
        current_pick.save()

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

    # 📤 GET
    return render(request, "league/make_pick.html", {
        "draft": draft,
        "current_pick": current_pick,
        "available_players": available_players,
        "teams": teams,
        "q": query,
        "pos": pos,
        "team": team,
        "error": None,
    })
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from league.models import Draft, Team, DraftPick

@login_required
def team_roster(request, team_id: int, draft_id: int = 1):
    team = get_object_or_404(Team, id=team_id)
    draft = get_object_or_404(Draft, id=draft_id)

    # Pega todos os jogadores já draftados por esse time nesse draft
    picks = (
        DraftPick.objects
        .select_related("player")
        .filter(draft=draft, team=team, player__isnull=False)
        .order_by("overall_number")
    )

    players = [p.player for p in picks if p.player]

    # Separar por posição (opcional, mas fica bonito)
    grouped = {
        "GOL": [pl for pl in players if pl.position == "GOL"],
        "LAT": [pl for pl in players if pl.position == "LAT"],
        "ZAG": [pl for pl in players if pl.position == "ZAG"],
        "MEI": [pl for pl in players if pl.position == "MEI"],
        "ATA": [pl for pl in players if pl.position == "ATA"],
        "TEC": [pl for pl in players if pl.position == "TEC"],
    }

    return render(request, "league/team_roster.html", {
        "team": team,
        "draft": draft,
        "players": players,
        "grouped": grouped,
    })
from django.urls import path
from . import views

urlpatterns = [
    # ... suas rotas atuais ...

    path("team/<int:team_id>/roster/", views.team_roster, name="team_roster"),
]
