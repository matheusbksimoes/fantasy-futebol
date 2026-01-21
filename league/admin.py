from django.contrib import admin
from .models import League, Team, Player, Roster, Matchup, Score, Draft, DraftPick

admin.site.register(League)
admin.site.register(Team)
admin.site.register(Player)
admin.site.register(Roster)
admin.site.register(Matchup)
admin.site.register(Score)
admin.site.register(Draft)
admin.site.register(DraftPick)
