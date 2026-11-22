from rest_framework.permissions import BasePermission

from .models import UserProfile


class IsAdminUser(BasePermission):
    """Allows access only to users whose profile role is admin."""

    message = "Admin role required."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        try:
            return user.profile.role == UserProfile.Role.ADMIN
        except UserProfile.DoesNotExist:
            return False
