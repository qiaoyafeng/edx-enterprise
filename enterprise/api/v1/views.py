# -*- coding: utf-8 -*-
"""
Views for enterprise api version 1 endpoint.
"""

from logging import getLogger
from smtplib import SMTPException

from django_filters.rest_framework import DjangoFilterBackend
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import filters, permissions, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action, detail_route, list_route
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_500_INTERNAL_SERVER_ERROR
from rest_framework.views import APIView
from rest_framework_xml.renderers import XMLRenderer
from six.moves.urllib.parse import quote_plus, unquote  # pylint: disable=import-error,ungrouped-imports

from django.apps import apps
from django.conf import settings
from django.core import mail
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext as _

from enterprise import models
from enterprise.api.filters import (
    EnterpriseCustomerUserFilterBackend,
    EnterpriseLinkedUserFilterBackend,
    UserFilterBackend,
)
from enterprise.api.throttles import ServiceUserThrottle
from enterprise.api.utils import (
    create_message_body,
    get_ent_cust_from_report_config_uuid,
    get_enterprise_customer_from_catalog_id,
    get_enterprise_customer_from_user_id,
)
from enterprise.api.v1 import serializers
from enterprise.api.v1.decorators import require_at_least_one_query_parameter
from enterprise.api.v1.permissions import IsInEnterpriseGroup
from enterprise.api_client.lms import EnrollmentApiClient
from enterprise.constants import COURSE_KEY_URL_PATTERN, CourseModes
from enterprise.errors import CodesAPIRequestError
from enterprise.utils import NotConnectedToOpenEdX, get_request_value
from enterprise_learner_portal.utils import CourseRunProgressStatuses, get_course_run_status

try:
    from lms.djangoapps.certificates.api import get_certificate_for_user
    from openedx.core.djangoapps.content.course_overviews.api import get_course_overviews
except ImportError:
    get_course_overviews = None
    get_certificate_for_user = None


LOGGER = getLogger(__name__)


class EnterpriseViewSet:
    """
    Base class for all Enterprise view sets.
    """

    permission_classes = (permissions.IsAuthenticated,)
    authentication_classes = (JwtAuthentication, SessionAuthentication,)
    throttle_classes = (ServiceUserThrottle,)

    def ensure_data_exists(self, request, data, error_message=None):
        """
        Ensure that the wrapped API client's response brings us valid data. If not, raise an error and log it.
        """
        if not data:
            error_message = (
                error_message or "Unable to fetch API response from endpoint '{}'.".format(request.get_full_path())
            )
            LOGGER.error(error_message)
            raise NotFound(error_message)


class EnterpriseWrapperApiViewSet(EnterpriseViewSet, viewsets.ViewSet):
    """
    Base class for attribute and method definitions common to all view sets which wrap external APIs.
    """


class EnterpriseModelViewSet(EnterpriseViewSet):
    """
    Base class for attribute and method definitions common to all view sets.
    """

    filter_backends = (filters.OrderingFilter, DjangoFilterBackend, UserFilterBackend,)
    permission_classes = (permissions.IsAuthenticated, permissions.DjangoModelPermissions,)
    USER_ID_FILTER = 'id'


class EnterpriseReadOnlyModelViewSet(EnterpriseModelViewSet, viewsets.ReadOnlyModelViewSet):
    """
    Base class for all read only Enterprise model view sets.
    """


class EnterpriseReadWriteModelViewSet(EnterpriseModelViewSet, viewsets.ModelViewSet):
    """
    Base class for all read/write Enterprise model view sets.
    """

    permission_classes = (permissions.IsAuthenticated, permissions.DjangoModelPermissions,)


class EnterpriseCustomerViewSet(EnterpriseReadWriteModelViewSet):
    """
    API views for the ``enterprise-customer`` API endpoint.
    """

    queryset = models.EnterpriseCustomer.active_customers.all()
    serializer_class = serializers.EnterpriseCustomerSerializer
    filter_backends = EnterpriseReadWriteModelViewSet.filter_backends + (EnterpriseLinkedUserFilterBackend,)

    USER_ID_FILTER = 'enterprise_customer_users__user_id'
    FIELDS = (
        'uuid', 'slug', 'name', 'active', 'site', 'enable_data_sharing_consent',
        'enforce_data_sharing_consent',
    )
    filterset_fields = FIELDS
    ordering_fields = FIELDS

    def get_serializer_class(self):
        if self.action == 'basic_list':
            return serializers.EnterpriseCustomerBasicSerializer
        return self.serializer_class

    @list_route()
    # pylint: disable=invalid-name,unused-argument
    def basic_list(self, request, *arg, **kwargs):
        """
            Enterprise Customer's Basic data list without pagination
        """
        startswith = request.GET.get('startswith')
        queryset = self.get_queryset().order_by('name')
        if startswith:
            queryset = queryset.filter(name__istartswith=startswith)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @method_decorator(require_at_least_one_query_parameter('course_run_ids', 'program_uuids'))
    @detail_route()
    @permission_required('enterprise.can_view_catalog', fn=lambda request, pk, course_run_ids, program_uuids: pk)
    # pylint: disable=invalid-name,unused-argument
    def contains_content_items(self, request, pk, course_run_ids, program_uuids):
        """
        Return whether or not the specified content is available to the EnterpriseCustomer.

        Multiple course_run_ids and/or program_uuids query parameters can be sent to this view to check
        for their existence in the EnterpriseCustomerCatalogs associated with this EnterpriseCustomer.
        At least one course run key or program UUID value must be included in the request.
        """
        enterprise_customer = self.get_object()

        # Maintain plus characters in course key.
        course_run_ids = [unquote(quote_plus(course_run_id)) for course_run_id in course_run_ids]

        contains_content_items = False
        for catalog in enterprise_customer.enterprise_customer_catalogs.all():
            contains_course_runs = not course_run_ids or catalog.contains_courses(course_run_ids)
            contains_program_uuids = not program_uuids or catalog.contains_programs(program_uuids)
            if contains_course_runs and contains_program_uuids:
                contains_content_items = True
                break

        return Response({'contains_content_items': contains_content_items})

    @detail_route(methods=['post'], permission_classes=[permissions.IsAuthenticated])
    @permission_required('enterprise.can_enroll_learners', fn=lambda request, pk: pk)
    # pylint: disable=invalid-name,unused-argument
    def course_enrollments(self, request, pk):
        """
        Creates a course enrollment for an EnterpriseCustomerUser.
        """
        enterprise_customer = self.get_object()
        serializer = serializers.EnterpriseCustomerCourseEnrollmentsSerializer(
            data=request.data,
            many=True,
            context={
                'enterprise_customer': enterprise_customer,
                'request_user': request.user,
            }
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=HTTP_200_OK)

        return Response(serializer.errors, status=HTTP_400_BAD_REQUEST)

    @method_decorator(require_at_least_one_query_parameter('permissions'))
    @list_route(permission_classes=[permissions.IsAuthenticated, IsInEnterpriseGroup])
    def with_access_to(self, request, *args, **kwargs):  # pylint: disable=invalid-name,unused-argument
        """
        Returns the list of enterprise customers the user has a specified group permission access to.
        """
        self.queryset = self.queryset.order_by('name')
        enterprise_id = self.request.query_params.get('enterprise_id', None)
        enterprise_slug = self.request.query_params.get('enterprise_slug', None)
        enterprise_name = self.request.query_params.get('search', None)

        if enterprise_id is not None:
            self.queryset = self.queryset.filter(uuid=enterprise_id)
        elif enterprise_slug is not None:
            self.queryset = self.queryset.filter(slug=enterprise_slug)
        elif enterprise_name is not None:
            self.queryset = self.queryset.filter(name__icontains=enterprise_name)
        return self.list(request, *args, **kwargs)

    @list_route()
    @permission_required('enterprise.can_access_admin_dashboard')
    def dashboard_list(self, request, *args, **kwargs):  # pylint: disable=invalid-name,unused-argument
        """
        Supports listing dashboard enterprises for frontend-app-admin-portal.
        """
        self.queryset = self.queryset.order_by('name')
        enterprise_id = self.request.query_params.get('enterprise_id', None)
        enterprise_slug = self.request.query_params.get('enterprise_slug', None)
        enterprise_name = self.request.query_params.get('search', None)

        if enterprise_id is not None:
            self.queryset = self.queryset.filter(uuid=enterprise_id)
        elif enterprise_slug is not None:
            self.queryset = self.queryset.filter(slug=enterprise_slug)
        elif enterprise_name is not None:
            self.queryset = self.queryset.filter(name__icontains=enterprise_name)
        return self.list(request, *args, **kwargs)


class EnterpriseCourseEnrollmentViewSet(EnterpriseReadWriteModelViewSet):
    """
    API views for the ``enterprise-course-enrollment`` API endpoint.
    """

    queryset = models.EnterpriseCourseEnrollment.objects.all()

    USER_ID_FILTER = 'enterprise_customer_user__user_id'
    FIELDS = (
        'enterprise_customer_user', 'course_id'
    )
    filterset_fields = FIELDS
    ordering_fields = FIELDS

    def get_serializer_class(self):
        """
        Use a special serializer for any requests that aren't read-only.
        """
        if self.request.method in ('GET',):
            return serializers.EnterpriseCourseEnrollmentReadOnlySerializer
        return serializers.EnterpriseCourseEnrollmentWriteSerializer


class LicensedEnterpriseCourseEnrollmentViewSet(EnterpriseWrapperApiViewSet):
    """
    API views for the ``licensed-enterprise-course-enrollment`` API endpoint.
    """

    queryset = models.LicensedEnterpriseCourseEnrollment.objects.all()
    serializer_class = serializers.LicensedEnterpriseCourseEnrollmentReadOnlySerializer

    def _validate_license_revoke_data(self, request_data):
        """
        Ensures the request data contains the necessary information.

        Arguments:
            request_data (dict): A dictionary of data passed to the request
        """
        user_id = request_data.get('user_id')
        enterprise_id = request_data.get('enterprise_id')

        if not user_id or not enterprise_id:
            msg = 'user_id and enterprise_id must be provided.'
            return Response(msg, status=status.HTTP_400_BAD_REQUEST)

        return None

    @action(methods=['post'], detail=False)
    @permission_required('enterprise.can_access_admin_dashboard', fn=lambda request: request.data.get('enterprise_id'))
    def license_revoke(self, request, *args, **kwargs):
        """
        Changes the mode for a user's licensed enterprise course enrollments to the "audit" course mode.
        """
        if get_course_overviews is None or get_certificate_for_user is None:
            raise NotConnectedToOpenEdX(
                _('To use this endpoint, this package must be '
                  'installed in an Open edX environment.')
            )

        request_data = request.data.copy()
        self._validate_license_revoke_data(request_data)

        user_id = request_data.get('user_id')
        enterprise_id = request_data.get('enterprise_id')
        audit_mode = CourseModes.AUDIT

        enterprise_customer_user = get_object_or_404(
            models.EnterpriseCustomerUser,
            user_id=user_id,
            enterprise_customer=enterprise_id,
        )
        licensed_enrollments = self.queryset.filter(
            enterprise_course_enrollment__enterprise_customer_user=enterprise_customer_user
        )

        enrollments_by_course_id = {
            enrollment.enterprise_course_enrollment.course_id: enrollment.enterprise_course_enrollment
            for enrollment in licensed_enrollments
        }
        course_overviews = get_course_overviews(list(enrollments_by_course_id.keys()))

        enrollment_api_client = EnrollmentApiClient()
        for course_overview in course_overviews:
            course_run_id = course_overview.get('id')
            enterprise_enrollment = enrollments_by_course_id.get(course_run_id)
            certificate_info = get_certificate_for_user(enterprise_customer_user.username, course_run_id) or {}
            course_run_status = get_course_run_status(
                course_overview,
                certificate_info,
                enterprise_enrollment,
            )

            if course_run_status == CourseRunProgressStatuses.COMPLETED:
                # skip updating the enrollment mode for this course as it is already completed, either
                # meaning the user has earned a certificate or the course has ended.
                continue

            try:
                enrollment_api_client.update_course_enrollment_mode_for_user(
                    username=enterprise_customer_user.username,
                    course_id=course_run_id,
                    mode=audit_mode,
                )
                LOGGER.info(
                    'Updated LMS enrollment for User {user} and Enterprise {enterprise} in Course {course_id} '
                    'to Course Mode {mode}.'.format(
                        user=enterprise_customer_user.username,
                        enterprise=enterprise_id,
                        course_id=course_run_id,
                        mode=audit_mode,
                    )
                )
            except Exception as exc:  # pylint: disable=broad-except
                msg = (
                    'Unable to update LMS enrollment for User {user} and Enterprise {enterprise} in Course {course_id} '
                    'to Course Mode {mode}'.format(
                        user=enterprise_customer_user.username,
                        enterprise=enterprise_id,
                        course_id=course_run_id,
                        mode=audit_mode,
                    )
                )
                LOGGER.error('{msg}: {exc}'.format(msg=msg, exc=exc))
                return Response(msg, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # mark the licensed enterprise course enrollment as "revoked"
            licensed_enrollment = enterprise_enrollment.license
            licensed_enrollment.is_revoked = True
            licensed_enrollment.save()

            # mark the enterprise course enrollment as "saved for later"
            enterprise_enrollment.saved_for_later = True
            enterprise_enrollment.save()

        return Response(status=status.HTTP_204_NO_CONTENT)


class EnterpriseCustomerUserViewSet(EnterpriseReadWriteModelViewSet):
    """
    API views for the ``enterprise-learner`` API endpoint.
    """

    queryset = models.EnterpriseCustomerUser.objects.all()
    filter_backends = (filters.OrderingFilter, DjangoFilterBackend, EnterpriseCustomerUserFilterBackend)

    FIELDS = (
        'enterprise_customer', 'user_id', 'active',
    )
    filterset_fields = FIELDS
    ordering_fields = FIELDS

    def get_serializer_class(self):
        """
        Use a flat serializer for any requests that aren't read-only.
        """
        if self.request.method in ('GET',):
            return serializers.EnterpriseCustomerUserReadOnlySerializer
        return serializers.EnterpriseCustomerUserWriteSerializer


class PendingEnterpriseCustomerUserViewSet(EnterpriseReadWriteModelViewSet):
    """
    API views for the ``pending-enterprise-learner`` API endpoint.
    """

    queryset = models.PendingEnterpriseCustomerUser.objects.all()
    filter_backends = (filters.OrderingFilter, DjangoFilterBackend)
    serializer_class = serializers.PendingEnterpriseCustomerUserSerializer
    permission_classes = (permissions.IsAuthenticated, permissions.IsAdminUser)

    FIELDS = (
        'enterprise_customer', 'user_email',
    )
    filterset_fields = FIELDS
    ordering_fields = FIELDS

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as validation_error:
            return_status = None
            if 'user_email' in serializer.errors:
                for error in serializer.errors['user_email']:
                    # This error indicates that a PendingEnterpriseCustomerUser already exists.
                    if error.code == 'unique':
                        return_status = status.HTTP_204_NO_CONTENT
                        break
            elif 'non_field_errors' in serializer.errors:
                for error in serializer.errors['non_field_errors']:
                    # This error indicates that an EnterpriseCustomerUser already exists.
                    if str(error) == 'EnterpriseCustomerUser record already exists':
                        return_status = status.HTTP_204_NO_CONTENT
                        break

            if not return_status:
                raise validation_error
        else:
            created = serializer.save()
            return_status = status.HTTP_201_CREATED if created else status.HTTP_204_NO_CONTENT
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=return_status, headers=headers)


class EnterpriseCustomerBrandingConfigurationViewSet(EnterpriseReadOnlyModelViewSet):
    """
    API views for the ``enterprise-customer-branding`` API endpoint.
    """

    queryset = models.EnterpriseCustomerBrandingConfiguration.objects.all()
    serializer_class = serializers.EnterpriseCustomerBrandingConfigurationSerializer

    USER_ID_FILTER = 'enterprise_customer__enterprise_customer_users__user_id'
    FIELDS = (
        'enterprise_customer__slug',
    )
    filterset_fields = FIELDS
    ordering_fields = FIELDS
    lookup_field = 'enterprise_customer__slug'


class EnterpriseCustomerCatalogViewSet(EnterpriseReadOnlyModelViewSet):
    """
    API Views for performing search through course discovery at the ``enterprise_catalogs`` API endpoint.
    """
    queryset = models.EnterpriseCustomerCatalog.objects.all()

    USER_ID_FILTER = 'enterprise_customer__enterprise_customer_users__user_id'
    FIELDS = (
        'uuid', 'enterprise_customer',
    )
    filterset_fields = FIELDS
    ordering_fields = FIELDS
    renderer_classes = (JSONRenderer, XMLRenderer,)

    @permission_required('enterprise.can_view_catalog', fn=lambda request, *args, **kwargs: None)
    def list(self, request, *args, **kwargs):
        return super(EnterpriseCustomerCatalogViewSet, self).list(request, *args, **kwargs)  # pylint: disable=no-member

    @permission_required(
        'enterprise.can_view_catalog',
        fn=lambda request, *args, **kwargs: get_enterprise_customer_from_catalog_id(kwargs['pk']))
    def retrieve(self, request, *args, **kwargs):
        return super(EnterpriseCustomerCatalogViewSet, self).retrieve(request, *args, **kwargs)

    def get_serializer_class(self):
        view_action = getattr(self, 'action', None)
        if view_action == 'retrieve':
            return serializers.EnterpriseCustomerCatalogDetailSerializer
        return serializers.EnterpriseCustomerCatalogSerializer

    @method_decorator(require_at_least_one_query_parameter('course_run_ids', 'program_uuids'))
    @detail_route()
    # pylint: disable=invalid-name,unused-argument
    def contains_content_items(self, request, pk, course_run_ids, program_uuids):
        """
        Return whether or not the EnterpriseCustomerCatalog contains the specified content.

        Multiple course_run_ids and/or program_uuids query parameters can be sent to this view to check
        for their existence in the EnterpriseCustomerCatalog. At least one course run key
        or program UUID value must be included in the request.
        """
        enterprise_customer_catalog = self.get_object()

        # Maintain plus characters in course key.
        course_run_ids = [unquote(quote_plus(course_run_id)) for course_run_id in course_run_ids]

        contains_content_items = True
        if course_run_ids:
            contains_content_items = enterprise_customer_catalog.contains_courses(course_run_ids)
        if program_uuids:
            contains_content_items = (
                contains_content_items and
                enterprise_customer_catalog.contains_programs(program_uuids)
            )

        return Response({'contains_content_items': contains_content_items})

    @detail_route(url_path='courses/{}'.format(COURSE_KEY_URL_PATTERN))
    @permission_required(
        'enterprise.can_view_catalog',
        fn=lambda request, pk, course_key: get_enterprise_customer_from_catalog_id(pk))
    def course_detail(self, request, pk, course_key):  # pylint: disable=invalid-name,unused-argument
        """
        Return the metadata for the specified course.

        The course needs to be included in the specified EnterpriseCustomerCatalog
        in order for metadata to be returned from this endpoint.
        """
        enterprise_customer_catalog = self.get_object()
        course = enterprise_customer_catalog.get_course(course_key)
        if not course:
            error_message = _(
                '[Enterprise API] CourseKey not found in the Catalog. Course: {course_key}, Catalog: {catalog_id}'
            ).format(
                course_key=course_key,
                catalog_id=enterprise_customer_catalog.uuid,
            )
            LOGGER.warning(error_message)
            raise Http404

        context = self.get_serializer_context()
        context['enterprise_customer_catalog'] = enterprise_customer_catalog
        serializer = serializers.CourseDetailSerializer(course, context=context)
        return Response(serializer.data)

    @detail_route(url_path='course_runs/{}'.format(settings.COURSE_ID_PATTERN))
    @permission_required(
        'enterprise.can_view_catalog',
        fn=lambda request, pk, course_id: get_enterprise_customer_from_catalog_id(pk))
    def course_run_detail(self, request, pk, course_id):  # pylint: disable=invalid-name,unused-argument
        """
        Return the metadata for the specified course run.

        The course run needs to be included in the specified EnterpriseCustomerCatalog
        in order for metadata to be returned from this endpoint.
        """
        enterprise_customer_catalog = self.get_object()
        course_run = enterprise_customer_catalog.get_course_run(course_id)
        if not course_run:
            error_message = _(
                '[Enterprise API] CourseRun not found in the Catalog. CourseRun: {course_id}, Catalog: {catalog_id}'
            ).format(
                course_id=course_id,
                catalog_id=enterprise_customer_catalog.uuid,
            )
            LOGGER.warning(error_message)
            raise Http404

        context = self.get_serializer_context()
        context['enterprise_customer_catalog'] = enterprise_customer_catalog
        serializer = serializers.CourseRunDetailSerializer(course_run, context=context)
        return Response(serializer.data)

    @detail_route(url_path='programs/(?P<program_uuid>[^/]+)')
    @permission_required(
        'enterprise.can_view_catalog',
        fn=lambda request, pk, program_uuid: get_enterprise_customer_from_catalog_id(pk))
    def program_detail(self, request, pk, program_uuid):  # pylint: disable=invalid-name,unused-argument
        """
        Return the metadata for the specified program.

        The program needs to be included in the specified EnterpriseCustomerCatalog
        in order for metadata to be returned from this endpoint.
        """
        enterprise_customer_catalog = self.get_object()
        program = enterprise_customer_catalog.get_program(program_uuid)
        if not program:
            error_message = _(
                '[Enterprise API] Program not found in the Catalog. Program: {program_uuid}, Catalog: {catalog_id}'
            ).format(
                program_uuid=program_uuid,
                catalog_id=enterprise_customer_catalog.uuid,
            )
            LOGGER.warning(error_message)
            raise Http404

        context = self.get_serializer_context()
        context['enterprise_customer_catalog'] = enterprise_customer_catalog
        serializer = serializers.ProgramDetailSerializer(program, context=context)
        return Response(serializer.data)


class EnterpriseCustomerReportingConfigurationViewSet(EnterpriseReadWriteModelViewSet):
    """
    API views for the ``enterprise-customer-reporting`` API endpoint.
    """

    queryset = models.EnterpriseCustomerReportingConfiguration.objects.all()
    serializer_class = serializers.EnterpriseCustomerReportingConfigurationSerializer
    lookup_field = 'uuid'
    permission_classes = [permissions.IsAuthenticated]

    USER_ID_FILTER = 'enterprise_customer__enterprise_customer_users__user_id'
    FIELDS = (
        'enterprise_customer',
    )
    filterset_fields = FIELDS
    ordering_fields = FIELDS

    @permission_required(
        'enterprise.can_manage_reporting_config',
        fn=lambda request, *args, **kwargs: get_ent_cust_from_report_config_uuid(kwargs['uuid']))
    def retrieve(self, request, *args, **kwargs):
        # pylint: disable=no-member
        return super(EnterpriseCustomerReportingConfigurationViewSet, self).retrieve(request, *args, **kwargs)

    @permission_required(
        'enterprise.can_manage_reporting_config',
        fn=lambda request, *args, **kwargs: get_enterprise_customer_from_user_id(request.user.id))
    def list(self, request, *args, **kwargs):
        # pylint: disable=no-member
        return super(EnterpriseCustomerReportingConfigurationViewSet, self).list(request, *args, **kwargs)

    @permission_required(
        'enterprise.can_manage_reporting_config',
        fn=lambda request, *args, **kwargs: get_enterprise_customer_from_user_id(request.user.id))
    def create(self, request, *args, **kwargs):
        config_data = request.data.copy()
        config_data['enterprise_customer_id'] = get_enterprise_customer_from_user_id(request.user.id)
        serializer = self.get_serializer(data=config_data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @permission_required(
        'enterprise.can_manage_reporting_config',
        fn=lambda request, *args, **kwargs: get_ent_cust_from_report_config_uuid(kwargs['uuid']))
    def update(self, request, *args, **kwargs):
        # pylint: disable=no-member
        return super(EnterpriseCustomerReportingConfigurationViewSet, self).update(request, *args, **kwargs)

    @permission_required(
        'enterprise.can_manage_reporting_config',
        fn=lambda request, *args, **kwargs: get_ent_cust_from_report_config_uuid(kwargs['uuid']))
    def partial_update(self, request, *args, **kwargs):
        # pylint: disable=no-member
        return super(EnterpriseCustomerReportingConfigurationViewSet, self).partial_update(request, *args, **kwargs)

    @permission_required(
        'enterprise.can_manage_reporting_config',
        fn=lambda request, *args, **kwargs: get_ent_cust_from_report_config_uuid(kwargs['uuid']))
    def destroy(self, request, *args, **kwargs):
        # pylint: disable=no-member
        return super(EnterpriseCustomerReportingConfigurationViewSet, self).destroy(request, *args, **kwargs)


class CatalogQueryView(APIView):
    """
    View for enterprise catalog query.
    This will be called from django admin tool to populate `content_filter` field of `EnterpriseCustomerCatalog` model.
    """
    authentication_classes = [SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]
    http_method_names = ['get']

    def get(self, request, catalog_query_id):
        """
        API endpoint for fetching an enterprise catalog query.
        """
        try:
            catalog_query = models.EnterpriseCatalogQuery.objects.get(pk=catalog_query_id)
        except models.EnterpriseCatalogQuery.DoesNotExist:
            return Response({"detail": "Could not find enterprise catalog query."}, status=HTTP_404_NOT_FOUND)
        return Response(catalog_query.content_filter, status=HTTP_200_OK)


class CouponCodesView(APIView):
    """
    API to request coupon codes.
    """
    permission_classes = (permissions.IsAuthenticated,)
    authentication_classes = (JwtAuthentication, SessionAuthentication,)
    throttle_classes = (ServiceUserThrottle,)

    REQUIRED_PARAM_EMAIL = 'email'
    REQUIRED_PARAM_ENTERPRISE_NAME = 'enterprise_name'
    OPTIONAL_PARAM_NUMBER_OF_CODES = 'number_of_codes'
    OPTIONAL_PARAM_NOTES = 'notes'

    MISSING_REQUIRED_PARAMS_MSG = "Some required parameter(s) missing: {}"

    def get_required_query_params(self, request):
        """
        Gets ``email``, ``enterprise_name``, ``number_of_codes``, and ``notes``,
        which are the relevant parameters for this API endpoint.

        :param request: The request to this endpoint.
        :return: The ``email``, ``enterprise_name``, ``number_of_codes`` and ``notes`` from the request.
        """
        email = get_request_value(request, self.REQUIRED_PARAM_EMAIL, '')
        enterprise_name = get_request_value(request, self.REQUIRED_PARAM_ENTERPRISE_NAME, '')
        number_of_codes = get_request_value(request, self.OPTIONAL_PARAM_NUMBER_OF_CODES, '')
        notes = get_request_value(request, self.OPTIONAL_PARAM_NOTES, '')
        if not (email and enterprise_name):
            raise CodesAPIRequestError(
                self.get_missing_params_message([
                    (self.REQUIRED_PARAM_EMAIL, bool(email)),
                    (self.REQUIRED_PARAM_ENTERPRISE_NAME, bool(enterprise_name)),
                ])
            )
        return email, enterprise_name, number_of_codes, notes

    def get_missing_params_message(self, parameter_state):
        """
        Get a user-friendly message indicating a missing parameter for the API endpoint.
        """
        params = ', '.join(name for name, present in parameter_state if not present)
        return self.MISSING_REQUIRED_PARAMS_MSG.format(params)

    @permission_required('enterprise.can_access_admin_dashboard')
    def post(self, request):
        """
        POST /enterprise/api/v1/request_codes

        Requires a JSON object of the following format:
        >>> {
        >>>     "email": "bob@alice.com",
        >>>     "enterprise_name": "IBM",
        >>>     "number_of_codes": "50",
        >>>     "notes": "Help notes for codes request",
        >>> }

        Keys:
        *email*
            Email of the customer who has requested more codes.
        *enterprise_name*
            The name of the enterprise requesting more codes.
        *number_of_codes*
            The number of codes requested.
        *notes*
            Help notes related to codes request.
        """
        try:
            email, enterprise_name, number_of_codes, notes = self.get_required_query_params(request)
        except CodesAPIRequestError as invalid_request:
            return Response({'error': str(invalid_request)}, status=HTTP_400_BAD_REQUEST)

        subject_line = _('Code Management - Request for Codes by {token_enterprise_name}').format(
            token_enterprise_name=enterprise_name
        )
        body_msg = create_message_body(email, enterprise_name, number_of_codes, notes)
        app_config = apps.get_app_config("enterprise")
        from_email_address = app_config.enterprise_integrations_email
        cs_email = app_config.customer_success_email
        data = {
            self.REQUIRED_PARAM_EMAIL: email,
            self.REQUIRED_PARAM_ENTERPRISE_NAME: enterprise_name,
            self.OPTIONAL_PARAM_NUMBER_OF_CODES: number_of_codes,
            self.OPTIONAL_PARAM_NOTES: notes,
        }
        try:
            messages_sent = mail.send_mail(
                subject_line,
                body_msg,
                from_email_address,
                [cs_email],
                fail_silently=False
            )
            LOGGER.info('[Enterprise API] Coupon code request emails sent: %s', messages_sent)
            return Response(data, status=HTTP_200_OK)
        except SMTPException:
            error_message = _(
                '[Enterprise API] Failure in sending e-mail to support.'
                ' SupportEmail: {token_cs_email}, UserEmail: {token_email}, EnterpriseName: {token_enterprise_name}'
            ).format(
                token_cs_email=cs_email,
                token_email=email,
                token_enterprise_name=enterprise_name
            )
            LOGGER.error(error_message)
            return Response(
                {'error': str('Request codes email could not be sent')},
                status=HTTP_500_INTERNAL_SERVER_ERROR
            )
