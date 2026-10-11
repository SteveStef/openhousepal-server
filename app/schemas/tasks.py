from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel

TaskKind = Literal['call', 'text', 'email', 'showing', 'general']
TaskPriority = Literal['low', 'normal', 'high']
# server-computed convenience bucket for the UI
TaskBucket = Literal['overdue', 'today', 'upcoming', 'someday', 'done']


class TaskCreate(BaseModel):
    title: str
    notes: Optional[str] = None
    kind: TaskKind = 'general'
    priority: TaskPriority = 'normal'
    due_at: Optional[datetime] = None
    all_day: bool = False
    remind_at: Optional[datetime] = None
    lead_id: Optional[str] = None
    suggested_message: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    kind: Optional[TaskKind] = None
    priority: Optional[TaskPriority] = None
    due_at: Optional[datetime] = None
    all_day: Optional[bool] = None
    remind_at: Optional[datetime] = None
    suggested_message: Optional[str] = None
    # true -> mark complete (stamps completed_at now); false -> reopen
    completed: Optional[bool] = None


class TaskLeadMini(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

    class Config:
        from_attributes = True


class TaskOut(BaseModel):
    id: str
    title: str
    notes: Optional[str] = None
    kind: TaskKind
    priority: TaskPriority
    due_at: Optional[datetime] = None
    all_day: bool
    remind_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    auto_source: Optional[str] = None
    suggested_message: Optional[str] = None
    # One line on why this follow-up exists ("Signed in at your open house - 12 Oak St")
    context: Optional[str] = None
    lead_id: Optional[str] = None
    lead: Optional[TaskLeadMini] = None
    # server-computed in the route after validation; default keeps model_validate(orm_obj) happy
    bucket: TaskBucket = 'someday'
    # True once this task has an event on the agent's connected Google/Outlook calendar
    on_calendar: bool = False
    # Public link to the lead's Showcase (lead without an agent + live Showcase only)
    showcase_url: Optional[str] = None
    # Showcase comments this person left (what they said, which home, agent-side link to it)
    comments: List[dict] = []
    # The home this follow-up is about: {address, short, url, where: showcase|spotlight|listing}
    property: Optional[dict] = None
    # The day(s) / time(s) they picked (showing / second visit / disclosure tour), as they gave them.
    requested_times: Optional[list[str]] = None
    # What they told you (form answers, their message) - same lines as the calendar alert's
    # "WHAT THEY TOLD YOU", minus the picked times (the card shows those on their own).
    details: Optional[list[str]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TaskCounts(BaseModel):
    overdue: int
    today: int
    upcoming: int
    someday: int
    open_total: int
