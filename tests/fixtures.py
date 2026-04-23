"""Minimal HTML fixtures that mirror the shape of Booked 4.1.x responses."""

LOGIN_PAGE_HTML = """<!doctype html>
<html><head><title>Booked - Log In</title></head>
<body>
  <form name="login" id="login" method="post" action="/Web/">
    <input type="text" name="email" />
    <input type="password" name="password" />
    <input type="hidden" name="captcha" value="" />
    <input type="hidden" name="resume" value="" />
    <button type="submit" name="login" value="submit">Log In</button>
  </form>
</body></html>
"""

DASHBOARD_HTML = """<!doctype html>
<html><head><title>Booked - Dashboard</title></head>
<body>
  <nav><a href="/Web/profile.php">Ben Goosman</a></nav>
  <main>Welcome back.</main>
</body></html>
"""

_FETCH_FORM = """
<form id="fetchReservationsForm">
  <input type="hidden" name="beginDate" value="2026-03-29"/>
  <input type="hidden" name="endDate" value="2026-04-04"/>
  <input type="hidden" name="scheduleId" value="3"/>
  <input type="hidden" name="LAST_REFRESH" value=""/>
  <input type="hidden" name="CSRF_TOKEN" value="TOKEN123"/>
</form>
"""

SCHEDULE_SHELL_HTML = f"""<!doctype html>
<html><head><title>Booked - Schedule</title></head>
<body>
  <h1 class="page-title">Primary Schedule</h1>
  <div class="schedule-date-range">Sun Mar 29, 2026</div>
  <select id="schedules">
    <option value="1" selected>Primary Schedule</option>
    <option value="2">Secondary</option>
  </select>
  <div id="reservations"><!-- filled by AJAX --></div>
  {_FETCH_FORM}
</body></html>
"""

SCHEDULE_SHELL_WITH_EMBED_HTML = SCHEDULE_SHELL_HTML.replace(
    "</body></html>",
    """<script>
    reservationResources.push({
        id: 2,
        href: "reservation?rid=2",
        name: "Room A",
    });
    </script></body></html>""",
)

# Booked 4.1.x PascalCase payload with Unix epoch StartDate/EndDate and
# display strings. 1774792800 = 2026-03-29 10:00:00 UTC,
# 1774796400 = 2026-03-29 11:00:00 UTC.
SCHEDULE_GRID_JSON = (
    '{"reservations":[{"ReferenceNumber":"rn1","Label":"Jam",'
    '"StartDate":"1774792800","EndDate":"1774796400",'
    '"StartDateString":"2026-03-29","EndDateString":"2026-03-29",'
    '"StartTime":"10:00 AM","EndTime":"11:00 AM",'
    '"ResourceId":2,"IsPending":false,"IsPast":false,"IsOwner":true,'
    '"IsParticipant":false,"IsBuffer":false,"IsReservation":true}],'
    '"lastRefresh":"2026-04-18T12:00:00+0000"}'
)

SCHEDULE_GRID_HTML = """
<table>
  <tr>
    <th class="resource" data-resource-id="7"><span class="resource-name">Squat Rack</span></th>
    <td class="past slot" data-start="2026-03-29T08:00:00" data-end="2026-03-29T09:00:00"></td>
    <td class="reservable slot" data-start="2026-03-29T09:00:00" data-end="2026-03-29T10:00:00"><a>Book</a></td>
    <td class="reserved mine slot" data-start="2026-03-29T10:00:00" data-end="2026-03-29T11:00:00" data-reference-number="abc123"><a>Morning lift</a></td>
  </tr>
  <tr>
    <th class="resource" data-resource-id="8"><span class="resource-name">Platform A</span></th>
    <td class="reservable slot" data-start="2026-03-29T09:00:00" data-end="2026-03-29T10:00:00"><a>Book</a></td>
    <td class="restricted slot" data-start="2026-03-29T10:00:00" data-end="2026-03-29T11:00:00"></td>
  </tr>
</table>
"""
