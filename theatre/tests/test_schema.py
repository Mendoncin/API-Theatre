from django.test import SimpleTestCase, override_settings
from drf_spectacular.validation import validate_schema
from rest_framework.test import APIClient


@override_settings(ALLOWED_HOSTS=["testserver"])
class OpenAPIContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        response = APIClient().get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        if response.status_code != 200:
            raise AssertionError(f"Schema endpoint returned {response.status_code}")
        cls.schema = response.json()
        cls.paths = cls.schema["paths"]

    def resolve(self, schema):
        if "$ref" in schema:
            return self.resolve(self.schema["components"]["schemas"][schema["$ref"].split("/")[-1]])
        if "allOf" in schema:
            return self.resolve(schema["allOf"][0])
        return schema

    def body(self, operation, status=None):
        body = operation["requestBody"] if status is None else operation["responses"][str(status)]
        return self.resolve(body["content"]["application/json"]["schema"])

    def test_schema_is_valid_and_swagger_is_served(self):
        self.assertTrue(self.schema["openapi"].startswith("3."))
        validate_schema(self.schema)
        self.assertEqual(APIClient().get("/api/docs/").status_code, 200)
        self.assertNotIn("/api/schema/", self.paths)

    def test_jwt_bearer_is_available_and_used(self):
        schemes = self.schema["components"]["securitySchemes"]
        jwt = {name for name, scheme in schemes.items()
               if scheme.get("type") == "http" and scheme.get("scheme") == "bearer"
               and scheme.get("bearerFormat", "").upper() == "JWT"}
        self.assertTrue(jwt)
        for method in ("get", "post"):
            security = self.paths["/api/theatre/reservations/"][method]["security"]
            self.assertTrue(any(jwt.intersection(item) for item in security))
            self.assertNotIn({}, security)

    def test_reservation_create_has_asymmetric_contract(self):
        operation = self.paths["/api/theatre/reservations/"]["post"]
        request = self.body(operation)["properties"]
        self.assertEqual(set(request), {"performance", "seats"})
        self.assertEqual(request["performance"]["type"], "integer")
        self.assertEqual(set(self.resolve(request["seats"]["items"])["properties"]), {"row", "seat"})
        response = self.body(operation, 201)["properties"]
        self.assertEqual(set(response), {"id", "user", "created_at", "performance", "tickets"})
        self.assertTrue(response["user"]["readOnly"])
        self.assertIn("play", self.resolve(response["performance"])["properties"])
        self.assertEqual(set(self.resolve(response["tickets"]["items"])["properties"]), {"id", "row", "seat"})

    def test_availability_is_public_direct_object_and_get_only(self):
        operations = self.paths["/api/theatre/performances/{id}/availability/"]
        self.assertEqual(set(operations), {"get"})
        operation = operations["get"]
        properties = self.body(operation, 200)["properties"]
        self.assertEqual(set(properties), {
            "performance", "theatre_hall", "capacity", "occupied_count",
            "available_count", "occupied_seats",
        })
        self.assertEqual(set(self.resolve(properties["theatre_hall"])["properties"]), {
            "id", "name", "rows", "seats_in_row",
        })
        self.assertEqual(set(self.resolve(properties["occupied_seats"]["items"])["properties"]), {"row", "seat"})
        self.assertNotIn("page", {p["name"] for p in operation.get("parameters", [])})
        self.assertFalse(operation.get("security"))

    def test_filters_are_documented(self):
        for resource, expected in (
            ("plays", {"search", "genre", "ordering", "page"}),
            ("performances", {"play", "date", "upcoming", "ordering", "page"}),
        ):
            parameters = {p["name"]: p for p in self.paths[f"/api/theatre/{resource}/"]["get"]["parameters"]}
            self.assertEqual(set(parameters), expected)
            if resource == "performances":
                self.assertEqual(parameters["upcoming"]["schema"]["type"], "boolean")

    def test_collections_are_paginated_and_details_are_direct(self):
        for resource in ("actors", "genres", "halls", "plays", "performances", "reservations", "tickets"):
            with self.subTest(resource=resource):
                base = f"/api/theatre/{resource}/"
                listing = self.body(self.paths[base]["get"], 200)["properties"]
                self.assertEqual(set(listing), {"count", "next", "previous", "results"})
                detail = self.body(self.paths[base + "{id}/"]["get"], 200)["properties"]
                self.assertIn("id", detail)
                self.assertNotIn("results", detail)
                if resource not in ("reservations", "tickets"):
                    self.assertFalse(self.paths[base]["get"].get("security"))
                    self.assertFalse(self.paths[base + "{id}/"]["get"].get("security"))
                    self.assertTrue(self.paths[base]["post"]["security"])

    def test_play_and_performance_read_write_contracts(self):
        play = "/api/theatre/plays/"
        listing = self.body(self.paths[play]["get"], 200)
        self.assertEqual(set(self.resolve(listing["properties"]["results"]["items"])["properties"]), {"id", "title", "genres"})
        detail = self.body(self.paths[play + "{id}/"]["get"], 200)["properties"]
        self.assertEqual(set(detail), {"id", "title", "description", "genres", "actors"})
        for field in ("actors", "genres"):
            self.assertIn("id", self.resolve(detail[field]["items"])["properties"])
        for resource, fields in (("plays", ("actors", "genres")), ("performances", ("play", "theatre_hall"))):
            base = f"/api/theatre/{resource}/"
            for method, path in (("post", base), ("put", base + "{id}/"), ("patch", base + "{id}/")):
                request = self.body(self.paths[path][method])["properties"]
                for field in fields:
                    value = request[field]
                    self.assertEqual(value.get("items", value)["type"], "integer")
        performance = self.body(self.paths["/api/theatre/performances/{id}/"]["get"], 200)["properties"]
        self.assertEqual(set(self.resolve(performance["play"])["properties"]), {"id", "title"})
        self.assertEqual(set(self.resolve(performance["theatre_hall"])["properties"]), {"id", "name"})

    def test_restricted_methods_and_owner_resource_ids(self):
        for resource, collection in (("reservations", {"get", "post"}), ("tickets", {"get"})):
            base = f"/api/theatre/{resource}/"
            self.assertEqual(set(self.paths[base]), collection)
            detail = self.paths[base + "{id}/"]
            self.assertEqual(set(detail), {"get", "delete"})
            for operation in detail.values():
                parameter = next(p for p in operation["parameters"] if p["name"] == "id")
                self.assertEqual(parameter["schema"]["type"], "integer")

    def test_domain_conflicts_preserve_success_responses(self):
        cases = [("/api/theatre/reservations/", "post", "201")]
        cases += [(f"/api/theatre/{resource}/{{id}}/", "delete", "204")
                  for resource in ("reservations", "tickets", "performances", "plays", "halls")]
        for path, method, success in cases:
            operation = self.paths[path][method]
            self.assertIn(success, operation["responses"])
            self.assertEqual(set(self.body(operation, 409)["properties"]), {"detail"})

    def test_registration_excludes_response_password_and_admin_input(self):
        operation = self.paths["/api/user/register/"]["post"]
        request = self.body(operation)["properties"]
        self.assertIn("password", request)
        self.assertTrue(request["password"]["writeOnly"])
        self.assertTrue({"is_staff", "is_superuser", "groups", "user_permissions"}.isdisjoint(request))
        self.assertEqual(set(self.body(operation, 201)["properties"]), {
            "id", "username", "first_name", "last_name", "email",
        })

    def test_jwt_token_endpoint_fields(self):
        obtain = self.paths["/api/user/token/"]["post"]
        refresh = self.paths["/api/user/token/refresh/"]["post"]
        self.assertEqual(set(self.body(obtain)["properties"]), {"username", "password"})
        self.assertEqual(set(self.body(obtain, 200)["properties"]), {"access", "refresh"})
        self.assertEqual(set(self.body(refresh)["properties"]), {"refresh"})
        self.assertEqual(set(self.body(refresh, 200)["properties"]), {"access"})
