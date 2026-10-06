from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


@override_settings(ALLOWED_HOSTS=["testserver"])
class UserEndpointContractTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.payload = {"username": "contract-user", "password": "Test-password-827!"}

    def test_registration_is_public_and_never_returns_password_or_privileges(self):
        response = self.client.post("/api/user/register/", {
            **self.payload, "is_staff": True, "is_superuser": True,
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.data), {"id", "username", "first_name", "last_name", "email"})
        user = get_user_model().objects.get(pk=response.data["id"])
        self.assertTrue(user.check_password(self.payload["password"]))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertNotIn(self.payload["password"], response.content.decode())
        self.assertNotIn(user.password, response.content.decode())

    def test_registration_validation_uses_field_errors(self):
        response = self.client.post("/api/user/register/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {"username", "password"})
        self.assertTrue(all(isinstance(errors, list) for errors in response.data.values()))

    def test_token_obtain_and_refresh_keep_their_intended_token_contract(self):
        get_user_model().objects.create_user(**self.payload)
        response = self.client.post("/api/user/token/", self.payload, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {"access", "refresh"})
        self.assertNotIn(self.payload["password"], response.content.decode())
        refreshed = self.client.post("/api/user/token/refresh/", {
            "refresh": response.data["refresh"],
        }, format="json")
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(set(refreshed.data), {"access"})

    def test_authentication_errors_do_not_echo_passwords_or_tokens(self):
        get_user_model().objects.create_user(**self.payload)
        cases = (
            ("token", {**self.payload, "password": "wrong-private-password"}, "wrong-private-password"),
            ("token/refresh", {"refresh": "invalid-private-token"}, "invalid-private-token"),
        )
        for endpoint, payload, secret in cases:
            with self.subTest(endpoint=endpoint):
                response = self.client.post(f"/api/user/{endpoint}/", payload, format="json")
                self.assertEqual(response.status_code, 401)
                self.assertIn("detail", response.data)
                self.assertNotIn(secret, response.content.decode())

    def test_user_endpoints_preserve_native_method_not_allowed(self):
        for endpoint in ("register", "token", "token/refresh"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(f"/api/user/{endpoint}/")
                self.assertEqual(response.status_code, 405)
                self.assertEqual(set(response.data), {"detail"})
                self.assertNotIn("GET", response["Allow"])
