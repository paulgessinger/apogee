from typing import NewType

from pydantic import BaseModel, Field

CommitHash = NewType("CommitHash", str)
URL = NewType("URL", str)


class CernUser(BaseModel):
    given_name: str
    family_name: str
    email: str
    cern_upn: str
    cern_mail_upn: str
    name: str = Field(..., alias="preferred_username")
