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


def _column_exists_sqlite(schema_editor, table_name: str, column_name: str) -> bool:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"PRAGMA table_info({table_name});")
        cols = [row[1] for row in cursor.fetchall()]  # row[1] = column name
    return column_name in cols


def ensure_waiver_priority_column(apps, schema_editor):
    """
    Garante que league_teambudget.waiver_priority existe.
    - SQLite não suporta IF EXISTS/IF NOT EXISTS em ALTER TABLE.
    - Postgres suporta, então usamos.
    """
    table = "league_teambudget"
    vendor = schema_editor.connection.vendor

    if vendor == "sqlite":
        if not _column_exists_sqlite(schema_editor, table, "waiver_priority"):
            schema_editor.execute(
                f"ALTER TABLE {table} ADD COLUMN waiver_priority INTEGER;"
            )
    else:
        # Postgres / MySQL etc.
        schema_editor.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS waiver_priority integer;"
        )


def drop_waiver_priority_column_reverse(apps, schema_editor):
    """
    Reverse da operação acima.
    - SQLite não suporta DROP COLUMN em versões antigas (e mesmo nas novas pode variar),
      então no SQLite fazemos noop pra não quebrar migrate reverso.
    """
    table = "league_teambudget"
    vendor = schema_editor.connection.vendor

    if vendor == "sqlite":
        return  # noop (reverse seguro)
    else:
        schema_editor.execute(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS waiver_priority;"
        )


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
        # ✅ 1) garante a coluna (SQLite-safe + Postgres-safe)
        migrations.RunPython(ensure_waiver_priority_column, drop_waiver_priority_column_reverse),

        # ✅ 2) seta prioridade inicial (só se estiver NULL)
        migrations.RunPython(set_initial_waiver_priorities, migrations.RunPython.noop),

        # ✅ 3) normaliza pra 1..N (e garante ordem limpa)
        migrations.RunPython(normalize_unique_sequence, migrations.RunPython.noop),
    ]
