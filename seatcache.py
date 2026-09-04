"""
A day-old-is-fine cache in front of Self-Service.

Seat counts move slowly enough that checking a course more than once a day
tells an adviser nothing new, and every lookup is load on a university system
we do not own. So each course carries the date it was last checked, and a
course checked today is served from disk.

The cache is keyed by course, not by student, which is what makes it pay: the
second and third advisee of the morning mostly share courses with the first.

Only public course and seat data is stored here. Nothing student-specific ever
reaches this file.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path

from selfservice import Course, Section, SelfServiceClient, SelfServiceError

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE / "cache" / "seats.json"


def today() -> str:
    return dt.date.today().isoformat()


class SeatCache:
    def __init__(
        self,
        path: Path = DEFAULT_PATH,
        *,
        client: SelfServiceClient | None = None,
    ) -> None:
        self.path = path
        self._client = client
        self.data = self._load()
        self.live_lookups = 0
        self.cache_hits = 0

    # -- persistence ------------------------------------------------------

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                data.setdefault("courses", {})
                data.setdefault("index", {})
                data.setdefault("terms", {})
                return data
            except (json.JSONDecodeError, OSError):
                pass  # A corrupt cache is not worth failing over; rebuild it.
        return {"courses": {}, "index": {}, "terms": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1))

    @property
    def client(self) -> SelfServiceClient:
        if self._client is None:
            self._client = SelfServiceClient()
        return self._client

    # -- freshness --------------------------------------------------------

    @staticmethod
    def _fresh(entry: dict | None) -> bool:
        return bool(entry) and entry.get("checked_on") == today()

    def is_fresh(self, code: str) -> bool:
        return self._fresh(self.data["courses"].get(code))

    # -- terms ------------------------------------------------------------

    def terms(self) -> list[dict]:
        """Every term Self-Service knows about, with dates where published.

        Deliberately not `discover_terms()`, which keeps the "Open Sections
        Only" filter on and so hides terms that have finished. We need labels
        for any term a cached section might sit in, past ones included.
        """
        entry = self.data["terms"]
        if self._fresh(entry):
            return entry["terms"]

        _, payload = self.client.search(
            open_only=False, page_size=1, complete=False
        )
        dated = {t.get("Code"): t for t in payload.get("ActivePlanTerms") or []}
        found = []
        for item in payload.get("TermFilters") or []:
            code = item.get("Value")
            if not code:
                continue
            extra = dated.get(code, {})
            found.append(
                {
                    "code": code,
                    "description": item.get("Description") or code,
                    "start": (extra.get("StartDate") or "")[:10],
                    "end": (extra.get("EndDate") or "")[:10],
                }
            )
        found.sort(key=lambda t: (t["start"] or "9999", t["code"]))
        self.data["terms"] = {"checked_on": today(), "terms": found}
        self.save()
        return found

    # -- the lookup -------------------------------------------------------

    def _refresh_index(self, subjects: list[str]) -> dict:
        """Map course code -> course id + section ids, for the subjects needed.

        One search covers every subject and every term at once: the endpoint
        takes a list of subjects, and leaving the term filter off makes
        sections come back bucketed by term.
        """
        entry = self.data["index"]
        known = set(entry.get("subjects") or []) if self._fresh(entry) else set()
        missing = sorted(set(subjects) - known)
        if not missing:
            return entry.get("courses") or {}

        courses, _ = self.client.search(
            subjects=sorted(known | set(missing)), terms=[], open_only=False
        )
        mapping = {
            course.code: {
                "id": course.course_id,
                "sections": course.matching_section_ids,
                "title": course.title,
                # Catalogue text, not seat data: it changes once a year, and
                # keeping it here means eligibility can be judged for a course
                # that has no sections to look up at all.
                "description": course.description,
                "requisites": [asdict(r) for r in course.requisites],
            }
            for course in courses
        }
        self.data["index"] = {
            "checked_on": today(),
            "subjects": sorted(known | set(missing)),
            "courses": mapping,
        }
        return mapping

    def lookup(self, codes: list[str]) -> dict[str, dict]:
        """Sections for each course code, hitting Self-Service only as needed.

        Returns {code: {"checked_on", "title", "sections": [...]}}. A course
        that Self-Service does not offer gets an empty section list, and is
        still marked checked so we don't ask again today.
        """
        wanted = sorted({c for c in codes if c})
        stale = [c for c in wanted if not self.is_fresh(c)]
        self.cache_hits += len(wanted) - len(stale)

        if stale:
            subjects = sorted({c.split("-")[0] for c in stale})
            index = self._refresh_index(subjects)

            for code in stale:
                found = index.get(code)
                entry = {
                    "checked_on": today(), "title": "", "sections": [],
                    "description": "", "requisites": [],
                }
                if found:
                    entry["description"] = found.get("description") or ""
                    entry["requisites"] = found.get("requisites") or []
                if found and found.get("sections"):
                    course = Course(
                        course_id=found["id"],
                        subject=code.split("-")[0],
                        number=code.split("-")[-1],
                        title=found.get("title") or "",
                        matching_section_ids=found["sections"],
                    )
                    try:
                        sections = self.client.fetch_sections(course)
                        self.live_lookups += 1
                    except SelfServiceError:
                        # Leave it stale so the next run tries again.
                        continue
                    entry["title"] = course.title
                    entry["sections"] = [
                        asdict(s) for s in sections
                        if s.has_open_seats or s.seats_unpublished
                    ]
                elif found:
                    entry["title"] = found.get("title") or ""
                self.data["courses"][code] = entry

            self.save()

        return {code: self.data["courses"].get(code, {
            "checked_on": None, "title": "", "sections": [],
            "description": "", "requisites": [],
        }) for code in wanted}


def sections_of(entry: dict) -> list[Section]:
    """Rehydrate stored sections into Section objects."""
    return [Section(**raw) for raw in entry.get("sections") or []]


if __name__ == "__main__":
    import sys

    codes = [c.upper().replace(" ", "-") for c in sys.argv[1:]] or [
        "BAN-610", "CIS-101"
    ]
    cache = SeatCache()
    results = cache.lookup(codes)
    print(
        f"{cache.cache_hits} from cache, {cache.live_lookups} looked up live\n"
    )
    for code, entry in results.items():
        sections = [s for s in sections_of(entry) if s.has_open_seats]
        print(f"{code}  {entry.get('title', '')[:40]}")
        if not sections:
            print("   no open sections")
        for section in sections:
            print(
                f"   {section.name:<14} {section.term_code:<7} "
                f"{section.seats_display:<12} {section.delivery}"
            )
