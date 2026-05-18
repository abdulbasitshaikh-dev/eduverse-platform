from django.conf import settings
from django.contrib.auth import get_user_model
from djoser.serializers import UserCreateSerializer, UserSerializer
from requests import Response
from rest_framework import serializers
from django.utils import timezone

from .validators import validate_document_file
from .models import (
    InstructorProfile,
    StudentProfile,
    User,
    InstructorVerificationDocument,
    VerificationAuditLog,
    VerificationSubmission,
)

User = get_user_model()


class CustomUserCreateSerializer(UserCreateSerializer):
    class Meta(UserCreateSerializer.Meta):
        model = User
        fields = ("id", "email", "username", "password", "role")


class CustomUserSerializer(UserSerializer):

    class Meta(UserSerializer.Meta):
        model = User
        fields = ("id", "email", "username", "role")
        extra_kwargs = {
            "role": {"read_only": True},
        }


class StudentProfileSerializer(serializers.ModelSerializer):
    user = CustomUserSerializer(read_only=True)

    class Meta:
        model = StudentProfile
        fields = ("user", "enrollment_date", "batch")


class VerificationDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstructorVerificationDocument
        fields = ["id", "document", "uploaded_at"]


class VerificationSubmissionAdminDetailSerializer(serializers.ModelSerializer):
    instructor_email = serializers.EmailField(
        source="profile.user.email", read_only=True
    )
    instructor_username = serializers.CharField(
        source="profile.user.username", read_only=True
    )
    instructor_bio = serializers.CharField(
        source="profile.bio", read_only=True)
    instructor_expertise = serializers.CharField(
        source="profile.expertise", read_only=True
    )

    documents = VerificationDocumentSerializer(many=True, read_only=True)

    class Meta:
        model = VerificationSubmission
        fields = [
            "id",
            "status",
            "rejection_reason",
            "created_at",
            "reviewed_at",
            "instructor_email",
            "instructor_username",
            "documents",
            "instructor_bio",
            "instructor_expertise",
        ]


class AdminProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "username", "role"]


class VerificationSubmissionSerializer(serializers.ModelSerializer):
    documents = VerificationDocumentSerializer(many=True, read_only=True)

    class Meta:
        model = VerificationSubmission
        fields = [
            "id",
            "status",
            "rejection_reason",
            "created_at",
            "documents",
        ]


class InstructorProfileSerializer(serializers.ModelSerializer):
    user = CustomUserSerializer(read_only=True)
    current_submission = serializers.SerializerMethodField()

    class Meta:
        model = InstructorProfile
        fields = ("id", "user", "bio", "expertise",
                  "is_verified", "current_submission")

        read_only_fields = (
            "is_verified",
            "verification_requested_at",
            "user",
        )

    def get_current_submission(self, obj):
        submission = obj.verification_submissions.filter(
            status=VerificationSubmission.STATUS_PENDING
        ).first()

        if not submission:
            return None

        return VerificationSubmissionSerializer(submission).data


class StudentRegisterSerializer(serializers.ModelSerializer):
    batch = serializers.CharField(required=False, write_only=True)

    class Meta:
        model = User
        fields = ("email", "username", "password", "batch")
        extra_kwargs = {"password": {"write_only": True}}

    def create(self, validated_data):
        batch = validated_data.pop("batch", "")

        user = User.objects.create_user(
            email=validated_data["email"],
            username=validated_data.get("username") or validated_data["email"],
            password=validated_data["password"],
            role=User.ROLE_STUDENT,
        )

        StudentProfile.objects.create(user=user, batch=batch)
        return user

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                "A user with this email already exists.")
        return value


class InstructorRegisterSerializer(serializers.ModelSerializer):
    bio = serializers.CharField(required=False, write_only=True)
    expertise = serializers.CharField(required=False, write_only=True)
    verification_documents = serializers.ListField(
        child=serializers.FileField(), write_only=True, allow_empty=False
    )

    class Meta:
        model = User
        fields = (
            "email",
            "username",
            "password",
            "bio",
            "expertise",
            "verification_documents",
        )
        extra_kwargs = {"password": {"write_only": True}}

    def create(self, validated_data):
        bio = validated_data.pop("bio", "")
        expertise = validated_data.pop("expertise", "")
        documents = validated_data.pop("verification_documents")

        user = User.objects.create_user(
            email=validated_data["email"],
            username=validated_data.get("username") or validated_data["email"],
            password=validated_data["password"],
            role=User.ROLE_INSTRUCTOR,
        )

        profile = InstructorProfile.objects.create(
            user=user,
            bio=bio,
            expertise=expertise,
        )

        submission = VerificationSubmission.objects.create(
            profile=profile, status=VerificationSubmission.STATUS_PENDING
        )

        for doc in documents:
            validate_document_file(doc)
            InstructorVerificationDocument.objects.create(
                submission=submission, document=doc
            )

        profile.verification_requested_at = timezone.now()
        profile.is_verified = False
        profile.save(update_fields=[
                     "verification_requested_at", "is_verified"])

        return user

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                "A user with this email already exists.")
        return value


class VerificationSubmissionAdminSerializer(serializers.ModelSerializer):
    instructor_email = serializers.EmailField(
        source="profile.user.email", read_only=True
    )

    class Meta:
        model = VerificationSubmission
        fields = [
            "id",
            "status",
            "created_at",
            "instructor_email",
        ]


class InstructorVerificationSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = VerificationSubmission
        fields = [
            "id",
            "status",
            "rejection_reason",
            "created_at",
        ]

    def validate(self, data):
        """
        Ensure an instructor doesn't have multiple pending verification submissions.
        """
        # Get the profile from the context if this is a create operation
        profile = self.context.get("profile")

        if profile:
            existing_pending = VerificationSubmission.objects.filter(
                profile=profile,
                status=VerificationSubmission.STATUS_PENDING
            ).exists()

            if existing_pending:
                raise serializers.ValidationError(
                    "Verification already under review. Please wait for the current submission to be reviewed."
                )

        return data


class RejectReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, allow_blank=False)


class EmptySerializer(serializers.Serializer):
    pass


class VerificationAuditLogSerializer(serializers.ModelSerializer):
    admin_email = serializers.EmailField(source="admin.email", read_only=True)

    class Meta:
        model = VerificationAuditLog
        fields = [
            "id",
            "action",
            "reason",
            "admin_email",
            "created_at",
        ]
