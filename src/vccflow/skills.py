"""Skills of a VCC account, through the public User Admin API (/useradmin/skills, version 4).

VCC Admin's own Users page calls the same API (userAdminAPI.har): GET /useradmin/skills?page=1&limit=500
lists {"skills": [{"skillId", "skillName", "skillDisplayId"}], "meta": {"page", "pageCount", ...}},
POST /useradmin/skills {"skillName", "description"} creates one and returns it, DELETE
/useradmin/skills/{skillId} answers 204. Skill names are not case-sensitive in VCC: with "DE",
"de" cannot be created.
"""

from __future__ import annotations

from vccflow.config import Account
from vccflow.scoped_api import ApiError, ScopedApi

ACCEPT_V4 = {"Accept": "application/vnd.newvoicemedia.v4+json"}
READ_SCOPES = ("users:read", "users:write")
WRITE_SCOPES = ("users:write",)
PAGE_SIZE = 500


class SkillsError(ApiError):
    pass


class SkillsClient(ScopedApi):
    error = SkillsError
    what = "skills"

    def __init__(self, account: Account, *args, **kwargs):
        super().__init__(account, *args, **kwargs)
        self.base = f"{account.manager_api_url}/useradmin/skills"

    def list(self) -> list[dict]:
        """All the account's skills: [{"skillId", "skillName", "skillDisplayId"}]."""
        skills, page = [], 1  # VCC Admin starts at page 1
        while True:
            body = self._call("GET", self.base, READ_SCOPES, headers=ACCEPT_V4,
                              params={"page": page, "limit": PAGE_SIZE}).json()
            skills += body.get("skills") or []
            if page >= int((body.get("meta") or {}).get("pageCount") or 1):
                return skills
            page += 1

    def names(self) -> list[str]:
        return [s["skillName"] for s in self.list()]

    def create(self, name: str, description: str = "") -> dict:
        """Create a skill, as VCC Admin does. Refused if the name exists in any letter case."""
        self._require_writes()
        clash = [s["skillName"] for s in self.list() if s["skillName"].lower() == name.lower()]
        if clash:
            raise SkillsError(f"A skill named '{clash[0]}' already exists in '{self.account.name}'")
        return self._call("POST", self.base, WRITE_SCOPES, headers=ACCEPT_V4,
                          json={"skillName": name, "description": description}).json()

    def delete(self, skill_id: str) -> None:
        self._require_writes()
        self._call("DELETE", f"{self.base}/{skill_id}", WRITE_SCOPES, headers=ACCEPT_V4)


# Groups: the same API (userAdminAPI.har). GET /useradmin/groups?page=1&limit=500 lists
# {"groups": [{"groupId", "groupName", "groupMaxUnexpectedTime", "groupMaxWrapUpTime", "groupDisplayId"}]};
# POST /useradmin/groups/ {"groupDisplayId", "groupName", "groupMaxUnexpectedTime", "groupMaxWrapUpTime"}
# creates one ("groupDisplayId": "" lets VCC choose it); DELETE /useradmin/groups/{groupId} answers 200.
# VCC Admin offered 500 seconds for both maximum times when the user created a group with defaults.
GROUP_DEFAULT_MAX_TIME = 500


class GroupsError(ApiError):
    pass


class GroupsClient(ScopedApi):
    error = GroupsError
    what = "groups"

    def __init__(self, account: Account, *args, **kwargs):
        super().__init__(account, *args, **kwargs)
        self.base = f"{account.manager_api_url}/useradmin/groups"

    def list(self) -> list[dict]:
        """All the account's groups."""
        groups, page = [], 1
        while True:
            body = self._call("GET", self.base, READ_SCOPES, headers=ACCEPT_V4,
                              params={"page": page, "limit": PAGE_SIZE}).json()
            groups += body.get("groups") or []
            if page >= int((body.get("meta") or {}).get("pageCount") or 1):
                return groups
            page += 1

    def create(self, display_id: str, name: str, max_unexpected: int = GROUP_DEFAULT_MAX_TIME,
               max_wrap_up: int = GROUP_DEFAULT_MAX_TIME) -> dict:
        """Create a group, as VCC Admin does (display_id "" lets VCC choose one)."""
        self._require_writes()
        return self._call("POST", f"{self.base}/", WRITE_SCOPES, headers=ACCEPT_V4,
                          json={"groupDisplayId": display_id, "groupName": name,
                                "groupMaxUnexpectedTime": max_unexpected, "groupMaxWrapUpTime": max_wrap_up}).json()

    def delete(self, group_id: str) -> None:
        self._require_writes()
        self._call("DELETE", f"{self.base}/{group_id}", WRITE_SCOPES, headers=ACCEPT_V4)
