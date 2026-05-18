from rest_framework.viewsets import ModelViewSet
from rest_framework.views import APIView
from rest_framework import generics, status, mixins, viewsets
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser, IsAuthenticated, AllowAny
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.filters import OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.db import transaction
from .permissions import IsInstructor, IsStudent, IsAdmin
from .serializers import (
    AdminProfileSerializer,
    InstructorProfileSerializer,
    InstructorVerificationSubmissionSerializer,
    StudentProfileSerializer,
    StudentRegisterSerializer,
    InstructorRegisterSerializer,
    RejectReasonSerializer,
    EmptySerializer,
    VerificationAuditLogSerializer,
    VerificationSubmissionAdminDetailSerializer,
    VerificationSubmissionAdminSerializer,
)
from .models import (
    StudentProfile,
    InstructorProfile,
    InstructorVerificationDocument,
    VerificationSubmission,
    VerificationAuditLog,
)
from .validators import validate_document_file

User = get_user_model()


class StudentRegisterView(mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = User.objects.all()
    serializer_class = StudentRegisterSerializer
    permission_classes = [AllowAny]


class InstructorRegisterView(mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = User.objects.all()
    serializer_class = InstructorRegisterSerializer
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser]


class ProfileDetail(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        user = self.request.user

        # Handle AnonymousUser for drf_spectacular schema generation
        if not user or user.is_anonymous:
            return StudentProfileSerializer

        if user.role == User.ROLE_ADMIN:
            return AdminProfileSerializer

        if user.role == User.ROLE_STUDENT:
            return StudentProfileSerializer

        return InstructorProfileSerializer

    def get_object(self):
        user = self.request.user

        # Handle AnonymousUser for drf_spectacular
        if not user or user.is_anonymous:
            return None

        # 🚨 CRITICAL: Admins do NOT get profiles
        if user.role == User.ROLE_ADMIN:
            return user

        if user.role == User.ROLE_STUDENT:
            profile, _ = StudentProfile.objects.get_or_create(user=user)
            return profile

        profile, _ = InstructorProfile.objects.get_or_create(user=user)
        return profile

    def destroy(self, request, *args, **kwargs):
        request.user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_update(self, serializer):
        serializer.save()

    def update(self, request, *args, **kwargs):
        """
        Prevent users from modifying restricted fields.
        Admin profile is read-only by design.
        """

        # 🚨 Admins should NOT update via this endpoint
        if request.user.role == User.ROLE_ADMIN:
            return Response(
                {"detail": "Admin profile cannot be modified."},
                status=status.HTTP_403_FORBIDDEN,
            )

        protected_keys = {"is_verified"}

        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer_class = self.get_serializer_class()

        if isinstance(request.data, QueryDict):
            data = request.data.copy()
        else:
            data = dict(request.data)

        for key in protected_keys:
            data.pop(key, None)

        serializer = serializer_class(
            instance,
            data=data,
            partial=partial,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return Response(serializer.data)


class AdminVerificationSubmissionViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAdmin]
    queryset = VerificationSubmission.objects.select_related(
        "profile", "profile__user"
    ).prefetch_related("documents")

    filter_backends = [DjangoFilterBackend, OrderingFilter]

    filterset_fields = ["status"]

    ordering_fields = ["created_at", "reviewed_at"]
    ordering = ["-created_at"]  # default: newest first

    def get_serializer_class(self):
        if self.action == "retrieve":
            return VerificationSubmissionAdminDetailSerializer
        return VerificationSubmissionAdminSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        submission = self.get_object()

        if submission.status != VerificationSubmission.STATUS_PENDING:
            return Response(
                {"detail": "Only pending submissions can be approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        submission.status = VerificationSubmission.STATUS_APPROVED
        submission.reviewed_at = timezone.now()
        submission.rejection_reason = ""
        submission.save()

        submission.profile.is_verified = True
        submission.profile.save(update_fields=["is_verified"])

        VerificationAuditLog.objects.create(
            submission=submission,
            admin=request.user,
            action=VerificationAuditLog.ACTION_APPROVED,
        )

        return Response({"detail": "Instructor verified."})

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        reason = request.data.get("rejection_reason")
        if not reason:
            return Response(
                {"rejection_reason": "This field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        submission = self.get_object()

        if submission.status != VerificationSubmission.STATUS_PENDING:
            return Response(
                {"detail": "Only pending submissions can be rejected."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        submission.status = VerificationSubmission.STATUS_REJECTED
        submission.reviewed_at = timezone.now()
        submission.rejection_reason = reason
        submission.save()

        VerificationAuditLog.objects.create(
            submission=submission,
            admin=request.user,
            action=VerificationAuditLog.ACTION_REJECTED,
            reason=reason,
        )

        return Response({"detail": "Submission rejected."})


class CreateVerificationSubmissionAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsInstructor]

    @transaction.atomic
    def post(self, request):
        user = request.user
        if user.role != User.ROLE_INSTRUCTOR:
            return Response(
                {"detail": "Only instructors can submit verification."},
                status=status.HTTP_403_FORBIDDEN,
            )

        profile = get_object_or_404(InstructorProfile, user=user)

        # block invalid states
        if profile.is_verified:
            return Response(
                {"detail": "Instructor already verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing_pending = VerificationSubmission.objects.filter(
            profile=profile, status=VerificationSubmission.STATUS_PENDING
        ).exists()

        if existing_pending:
            return Response(
                {"detail": "Verification already under review."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        files = request.FILES.getlist("verification_documents")
        if not files:
            return Response(
                {"detail": "verification_documents is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        submission = VerificationSubmission.objects.create(
            profile=profile, status=VerificationSubmission.STATUS_PENDING
        )

        for f in files:
            validate_document_file(f)
            InstructorVerificationDocument.objects.create(
                submission=submission, document=f
            )

        return Response(
            {"detail": "Verification submitted successfully."},
            status=status.HTTP_201_CREATED,
        )


class InstructorVerificationStatusAPIView(APIView):
    permission_classes = [IsInstructor]

    def get(self, request):
        user = request.user

        if user.role != User.ROLE_INSTRUCTOR:
            return Response(
                {"detail": "Only instructors can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        profile = get_object_or_404(InstructorProfile, user=user)

        # If already verified
        if profile.is_verified:
            return Response(
                {
                    "is_verified": True,
                    "current_submission": None,
                    "can_resubmit": False,
                }
            )

        # Try to find pending submission
        pending_submission = (
            VerificationSubmission.objects.filter(
                profile=profile, status=VerificationSubmission.STATUS_PENDING
            )
            .order_by("-created_at")
            .first()
        )

        if pending_submission:
            return Response(
                {
                    "is_verified": False,
                    "current_submission": InstructorVerificationSubmissionSerializer(
                        pending_submission
                    ).data,
                    "can_resubmit": False,
                }
            )

        # Otherwise, find latest rejected submission
        rejected_submission = (
            VerificationSubmission.objects.filter(
                profile=profile, status=VerificationSubmission.STATUS_REJECTED
            )
            .order_by("-created_at")
            .first()
        )

        if rejected_submission:
            return Response(
                {
                    "is_verified": False,
                    "current_submission": InstructorVerificationSubmissionSerializer(
                        rejected_submission
                    ).data,
                    "can_resubmit": True,
                }
            )

        # No submissions at all
        return Response(
            {
                "is_verified": False,
                "current_submission": None,
                "can_resubmit": True,
            }
        )


class AdminVerificationAuditLogAPIView(generics.ListAPIView):
    permission_classes = [IsAdmin]
    serializer_class = VerificationAuditLogSerializer

    def get_queryset(self):
        submission_id = self.kwargs["submission_id"]
        return VerificationAuditLog.objects.filter(
            submission_id=submission_id
        ).select_related("admin")
