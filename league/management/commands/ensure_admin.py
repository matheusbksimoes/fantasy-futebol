import os
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Create or update an admin user from environment variables."

    def handle(self, *args, **options):
        username = os.environ.get("ADMIN_USERNAME")
        password = os.environ.get("ADMIN_PASSWORD")
        email = os.environ.get("ADMIN_EMAIL", "")

        if not username or not password:
            self.stdout.write(self.style.WARNING(
                "ADMIN_USERNAME/ADMIN_PASSWORD not set. Skipping admin creation."
            ))
            return

        User = get_user_model()

        user, created = User.objects.get_or_create(username=username, defaults={"email": email})
        if email and user.email != email:
            user.email = email

        # garante flags de admin
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True

        # sempre garante a senha (isso resolve “não sei a senha do online”)
        user.set_password(password)
        user.save()

        if created:
            self.stdout.write(self.style.SUCCESS(f"Admin '{username}' created/updated."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Admin '{username}' updated."))
