from django.urls import path
from . import views

urlpatterns = [
    # draft
    path("draft/<int:draft_id>/board/", views.draft_board, name="draft_board"),
    path("draft/<int:draft_id>/start/", views.start_draft_view, name="start_draft_view"),
    path("draft/<int:draft_id>/pick/", views.make_pick, name="make_pick"),

    # roster
    path(
        "draft/<int:draft_id>/teams/<int:team_id>/roster/",
        views.team_roster,
        name="team_roster",
    ),
    path(
        "team/<int:team_id>/roster/",
        views.team_roster_legacy,
        name="team_roster_legacy",
    ),

    # 🔥 NOVA ROTA (drag and drop)
    path(
        "draft/<int:draft_id>/team/<int:team_id>/reorder-roster/",
        views.reorder_roster,
        name="reorder_roster",
    ),

    # geral
    path(
        "draft/<int:draft_id>/transactions/",
        views.transactions_list,
        name="transactions_list",
    ),
    path(
        "draft/<int:draft_id>/standings/",
        views.standings_view,
        name="standings_view",
    ),
    path(
        "draft/<int:draft_id>/notifications/",
        views.notifications_view,
        name="notifications_view",
    ),

    # lineup
    path(
        "draft/<int:draft_id>/teams/<int:team_id>/lineup/",
        views.set_lineup,
        name="set_lineup",
    ),
    path(
        "draft/<int:draft_id>/teams/<int:team_id>/lineup/week/<int:week_number>/",
        views.set_lineup,
        name="set_lineup_week",
    ),

    # free agents
    path(
        "teams/<int:team_id>/free-agents/",
        views.free_agents_list,
        name="free_agents_list",
    ),
    path(
        "teams/<int:team_id>/add/<int:player_id>/",
        views.add_free_agent,
        name="add_free_agent",
    ),
    path(
        "teams/<int:team_id>/drop/<int:player_id>/",
        views.drop_player,
        name="drop_player",
    ),

    # claims
    path(
        "teams/<int:team_id>/claims/",
        views.my_claims,
        name="my_claims",
    ),
    path(
        "teams/<int:team_id>/claims/<int:claim_id>/update/",
        views.update_claim,
        name="update_claim",
    ),
    path(
        "teams/<int:team_id>/claims/<int:claim_id>/cancel/",
        views.cancel_claim,
        name="cancel_claim",
    ),

        # semanas
    path(
        "draft/<int:draft_id>/weeks/postpone/",
        views.postpone_current_week,
        name="postpone_current_week",
    ),

    # 🔥 NOVA PÁGINA DE RODADAS
    path(
        "draft/<int:draft_id>/rounds/",
        views.rounds_view,
        name="rounds_view",
    ),

    path(
        "draft/<int:draft_id>/week/",
        views.current_week_view,
        name="current_week_matchups",
    ),
    path(
        "draft/<int:draft_id>/week/current/",
        views.current_week_view,
        name="current_week",
    ),

    # matchup detalhado
    path(
        "draft/<int:draft_id>/week/<int:week_number>/matchup/<int:matchup_id>/",
        views.matchup_detail,
        name="matchup_detail",
    ),

    # trade
    path(
        "draft/<int:draft_id>/teams/<int:target_team_id>/trade/player/<int:target_player_id>/",
        views.propose_trade_player,
        name="propose_trade_player",
    ),
    path(
        "trades/<int:trade_id>/accept/",
        views.accept_trade,
        name="accept_trade",
    ),
    path(
        "trades/<int:trade_id>/reject/",
        views.reject_trade,
        name="reject_trade",
    ),

    # player detail
    path(
        "players/<int:player_id>/",
        views.player_detail,
        name="player_detail",
    ),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("notifications/<int:notification_id>/delete/", views.delete_notification, name="delete_notification"),
    path("notifications/<int:notification_id>/delete/", views.delete_notification, name="delete_notification"),
path("notifications/clear-old/", views.clear_old_notifications, name="clear_old_notifications"),
path("notifications/clear-all/", views.clear_all_notifications, name="clear_all_notifications"),
]