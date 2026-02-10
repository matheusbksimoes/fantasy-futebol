# league/migrations/0020_fix_waiver_priority_column.py
from django.db import migrations


INITIAL_ORDER = [
    "Ade",
    "Marcelinho",
    "Mau",
    "André",
    "Isaac",
    "Victor",
    "Zinho",
    "Weyd",
    "Vicente",
    "Lorenzo",
]


def set_initial_waiver_priorities(apps, schema_editor):
    Team = apps.get_model("league", "Team")
    TeamBudget = apps.get_model("league", "TeamBudget")

    name_to_priority = {name: i + 1 for i, name in enumerate(INITIAL_ORDER)}

    # garante TeamBudget pra todo mundo
    for team in Team.objects.all():
        TeamBudget.objects.get_or_create(team_id=team.id, defaults={"faab_balance": 100})

    # seta a prioridade inicial apenas para quem está NULL (não sobrescreve se já existir)
    for budget in TeamBudget.objects.select_related("team").all():
        if budget.waiver_priority is not None:
            continue

        team_name = (budget.team.name or "").strip()
        prio = name_to_priority.get(team_name)

        # se não achar pelo nome, joga pro final (mas ainda assim determinístico)
        if prio is None:
            prio = 9999

        budget.waiver_priority = prio
        budget.save(update_fields=["waiver_priority"])


def normalize_unique_sequence(apps, schema_editor):
    """
    Deixa waiver_priority como 1..N sem buracos,
    mantendo a ordem relativa que existe hoje.
    """
    TeamBudget = apps.get_model("league", "TeamBudget")

    budgets = list(TeamBudget.objects.select_related("team").all())

    # ordena: primeiro quem tem número baixo, depois pelo id pra estabilidade
    budgets.sort(key=lambda b: ((b.waiver_priority if b.waiver_priority is not None else 10**9), b.id))

    for idx, b in enumerate(budgets, start=1):
        if b.waiver_priority != idx:
            b.waiver_priority = idx
            b.save(update_fields=["waiver_priority"])


class Migration(migrations.Migration):

    dependencies = [
        ("league", "0019_teambudget_waiver_priority_alter_waiverclaim_status_and_more"),
    ]

    operations = [
        # ✅ 1) garante a coluna no Postgres mesmo que o Django ache que já aplicou
        migrations.RunSQL(
            sql="""
                ALTER TABLE league_teambudget
                ADD COLUMN IF NOT EXISTS waiver_priority integer;
            """,
            reverse_sql="""
                ALTER TABLE league_teambudget
                DROP COLUMN IF EXISTS waiver_priority;
            """,
        ),

        # ✅ 2) seta prioridade inicial (só se estiver NULL)
        migrations.RunPython(set_initial_waiver_priorities, migrations.RunPython.noop),

        # ✅ 3) normaliza pra 1..N (e garante ordem limpa)
        migrations.RunPython(normalize_unique_sequence, migrations.RunPython.noop),
    ]
