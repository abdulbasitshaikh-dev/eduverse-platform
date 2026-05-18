from django.utils import timezone
from django.db import models
from django.db.models import Q
from django.contrib.auth.models import AbstractUser, BaseUserManager
from academy import settings


class CustomUserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("The given email must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    ROLE_STUDENT = "student"
    ROLE_INSTRUCTOR = "instructor"
    ROLE_ADMIN = "admin"

    ROLE_CHOICES = (
        (ROLE_STUDENT, "Student"),
        (ROLE_INSTRUCTOR, "Instructor"),
        (ROLE_ADMIN, "Admin"),
    )

    username = models.CharField(max_length=150)
    email = models.EmailField(unique=True)

    role = models.CharField(
        max_length=10, choices=ROLE_CHOICES, default=ROLE_STUDENT)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    objects = CustomUserManager()

    def __str__(self):
        return f"{self.email} ({self.role})"


class VerificationSubmission(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    profile = models.ForeignKey(
        "InstructorProfile",
        on_delete=models.CASCADE,
        related_name="verification_submissions",
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )

    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"VerificationSubmission({self.profile.user.email}, {self.status})"


class StudentProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="student_profile"
    )
    # enrolled_courses = models.ManyToManyField('courses.Course', blank=True)
    enrollment_date = models.DateField(auto_now_add=True)
    batch = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return f"Student Profile: {self.user.email}"


class InstructorProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="instructor_profile"
    )

    bio = models.TextField(blank=True, null=True)
    expertise = models.CharField(max_length=255, blank=True, null=True)

    # Profile-level verification state
    is_verified = models.BooleanField(default=False)
    verification_requested_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Instructor Profile: {self.user.email}"


class InstructorVerificationDocument(models.Model):
    submission = models.ForeignKey(
        VerificationSubmission, on_delete=models.CASCADE, related_name="documents"
    )

    document = models.FileField(upload_to="verification_documents/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Document({self.id})"


class VerificationAuditLog(models.Model):
    ACTION_APPROVED = "approved"
    ACTION_REJECTED = "rejected"

    ACTION_CHOICES = [
        (ACTION_APPROVED, "Approved"),
        (ACTION_REJECTED, "Rejected"),
    ]

    submission = models.ForeignKey(
        VerificationSubmission, on_delete=models.CASCADE, related_name="audit_logs"
    )

    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verification_actions",
    )

    action = models.CharField(max_length=20, choices=ACTION_CHOICES)

    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["submission", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.action} | submission={self.submission_id} | admin={self.admin_id}"
        )
