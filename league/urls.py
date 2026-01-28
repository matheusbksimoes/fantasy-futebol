from django.urls import path
from . import views

urlpatterns = [
    path("draft/<int:draft_id>/board/", views.draft_board, name="draft_board"),
    path("draft/<int:draft_id>/start/", views.start_draft_view, name="start_draft_view"),
    path("draft/<int:draft_id>/pick/", views.make_pick, name="make_pick"),

    # ✅ roster (rota nova)
    path("draft/<int:draft_id>/teams/<int:team_id>/roster/", views.team_roster, name="team_roster"),

    # ✅ roster (rota legacy — para /team/<id>/roster/ não dar 404)
    path("team/<int:team_id>/roster/", views.team_roster_legacy, name="team_roster_legacy"),

    path("teams/<int:team_id>/drop/<int:player_id>/", views.drop_player, name="drop_player"),

    path("teams/<int:team_id>/free-agents/", views.free_agents_list, name="free_agents_list"),
    path("teams/<int:team_id>/add/<int:player_id>/", views.add_free_agent, name="add_free_agent"),
    path("draft/<int:draft_id>/transactions/", views.transactions_list, name="transactions_list"),
    path("draft/<int:draft_id>/teams/<int:team_id>/lineup/", views.set_lineup, name="set_lineup"),
path(
    "draft/<int:draft_id>/weeks/postpone/",
    views.postpone_current_week,
    name="postpone_current_week",
),
path("draft/<int:draft_id>/week/", views.current_week_matchups, name="current_week_matchups"),
path("draft/<int:draft_id>/week/current/", views.current_week_view, name="current_week"),

]
