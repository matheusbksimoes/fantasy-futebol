from django.urls import path
from . import views

urlpatterns = [
    path("draft/<int:draft_id>/board/", views.draft_board, name="draft_board"),
    path("draft/<int:draft_id>/start/", views.start_draft_view, name="start_draft_view"),
    path("draft/<int:draft_id>/pick/", views.make_pick, name="make_pick"),

    path("team/<int:team_id>/roster/", views.team_roster, name="team_roster"),
]
