from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
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
    Lineup,          # ✅ novo
    LineupSpot,      # ✅ novo (lineup/slot_type/slot_index/player)
    PlayerWeekScore,
    FORMATION_MAP,   # ✅ novo
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
@login_required
def team_roster(request, draft_id: int, team_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

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

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

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

    # criar faltantes
    to_create = []
    for (t, idx) in expected:
        if (t, idx) not in existing:
            to_create.append(LineupSpot(lineup=lineup, slot_type=t, slot_index=idx, player=None))
    if to_create:
        LineupSpot.objects.bulk_create(to_create)

    # remover extras (só se vazio)
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

    # week alvo
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

    # Week travada: só admin mexe
    if week.is_locked and not request.user.is_superuser:
        return HttpResponseForbidden("Esta Week está travada. Não é possível editar a escalação.")

    round_number = week.number

    # 🔹 1) pega formação do PREVIEW (GET) se existir, senão usa a salva no banco
    preview_formation = request.GET.get("formation")

    # 🔹 2) garante que existe Lineup no banco (sempre com a formação salva)
    lineup, _ = Lineup.objects.get_or_create(
        week=week,
        team=team,
        defaults={"formation": "433"},
    )

    # 🔹 3) formação efetiva usada para MONTAR OS SLOTS NA TELA (preview)
    # ✅ PREVIEW de formação via GET (não salva no banco)
    preview_formation = request.GET.get("formation")
    selected_formation = preview_formation or lineup.formation
    if selected_formation not in FORMATION_MAP:
        selected_formation = lineup.formation

    # Roster disponível do time (ativos)
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
                # trocando formação: bloqueia se existir jogador travado já escalado
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

                    lineup.formation = new_formation
                    lineup.full_clean()
                    lineup.save(update_fields=["formation", "updated_at"])

                # ✅ garante slots REAIS da formação salva (agora sim)
                ensure_spots_for_lineup(lineup)
                expected = expected_slots_for_formation(lineup.formation)

                # map de slots atuais
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

            return redirect(
                "matchup_view",
                draft_id=draft.id,
                week_number=week.number,
                team_id=team.id,
            )

            if week_number is not None:
                return redirect("set_lineup_week", draft_id=draft.id, team_id=team.id, week_number=week_number)
            return redirect("set_lineup", draft_id=draft.id, team_id=team.id)

        except (ValidationError, Player.DoesNotExist) as e:
            messages.error(request, str(e))

    # ✅ GET: renderiza slots conforme selected_formation (preview), sem mexer no banco
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
                # objeto "fake" só pra render (sem id)
                slots.append(LineupSpot(lineup=lineup, slot_type=t, slot_index=idx, player=None))

    except ValidationError as e:
        messages.error(request, str(e))
        slots = list(LineupSpot.objects.filter(lineup=lineup).select_related("player"))

    slots.sort(key=lambda s: (SLOT_ORDER.get(s.slot_type, 99), s.slot_index or 999))

    locked_player_ids = set()
    # ✅ trava deve considerar os slots reais existentes (do banco)
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
        "selected_formation": selected_formation,  # ✅ IMPORTANTE pro template
        "slots": slots,
        "locked_player_ids": locked_player_ids,

        "gols": gols,
        "zags": zags,
        "lats": lats,
        "meis": meis,
        "atas": atas,
        "tecs": tecs,
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
from django.db.models import Q

def lineup_display_data(week, team):
    """
    Retorna (lineup, spots_ordenados, total_points, points_map)
    """
    lineup, _ = Lineup.objects.get_or_create(week=week, team=team, defaults={"formation": "433"})

    # garante slots reais do banco para formação salva
    try:
        ensure_spots_for_lineup(lineup)
    except ValidationError:
        pass

    spots = list(LineupSpot.objects.filter(lineup=lineup).select_related("player"))
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
def matchup_view(request, draft_id: int, week_number: int, team_id: int):
    draft = get_object_or_404(Draft, id=draft_id)
    week = get_object_or_404(Week, draft=draft, number=week_number)
    team = get_object_or_404(Team, id=team_id)

    forbidden = forbid_if_not_team_owner(request, team)
    if forbidden:
        return forbidden

    matchup = (
        Matchup.objects
        .filter(week=week)
        .filter(Q(home_team=team) | Q(away_team=team))
        .select_related("home_team", "away_team", "week")
        .first()
    )

    if not matchup:
        # se não tiver schedule ainda, volta pra week view
        messages.error(request, "Nenhum matchup encontrado para este time nesta week.")
        return redirect("current_week", draft_id=draft.id)

    home_lineup, home_spots, home_total, home_points_map = lineup_display_data(week, matchup.home_team)
    away_lineup, away_spots, away_total, away_points_map = lineup_display_data(week, matchup.away_team)

    return render(request, "league/matchup.html", {
        "draft": draft,
        "week": week,
        "team": team,  # pro base.html não quebrar (aba Meu roster etc.)
        "active_tab": "my_matchup",

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
    })
# league/views.py
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from league.models import Player, WaiverClaim

@require_POST
def create_waiver_claim(request, player_id):
    team = request.user.profile.team  # ajuste conforme seu auth
    add_player = get_object_or_404(Player, id=player_id)

    bid = int(request.POST.get("bid", 0))
    drop_player_id = request.POST.get("drop_player_id") or None

    drop_player = None
    if drop_player_id:
        drop_player = get_object_or_404(Player, id=drop_player_id)

    WaiverClaim.objects.create(
        team=team,
        add_player=add_player,
        drop_player=drop_player,
        bid=max(bid, 0),
    )

    messages.success(request, "Waiver claim criado! Vai ser processado no próximo horário.")
    return redirect("free_agents")
