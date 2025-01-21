from typing import NewType

from pydantic import BaseModel, Field
import dataclasses

CommitHash = NewType("CommitHash", str)
URL = NewType("URL", str)


class CernUserResponse(BaseModel):
    given_name: str
    family_name: str
    email: str
    cern_upn: str
    cern_mail_upn: str
    name: str = Field(..., alias="preferred_username")


@dataclasses.dataclass
class CernUser:
    name: str
    email: str
