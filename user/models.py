from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager


# Create your models here.

class User(AbstractUser):
    pass

class UserManager(BaseUserManager):
    pass