# Email Templates - Mailgun Configuration

## Template Names and Variables

### 1. `password_reset`
**Purpose:** Send password reset link to users

**Variables:**
- `reset_link` - The password reset URL with token

**Subject:** Reset Your Password - OpenHousePal

**Sent to:** User requesting password reset

**Location:** `app/api/auth_routes.py:380`

---

### 2. `agent_welcome`
**Purpose:** Welcome email sent to agents immediately after signup

**Variables:**
- `agent_name` - First name of the agent
- `plan_tier` - Subscription plan tier (BASIC, PREMIUM)

**Subject:** Welcome to OpenHousePal!

**Sent to:** Agent (new user)

**Location:** `app/api/auth_routes.py:151`

---

### 3. `visitor_confirmation`
**Purpose:** Confirmation email sent to visitors after completing open house sign-in form

**Variables:**
- `visitor_name` - Full name of the visitor
- `property_address` - Address of the property visited
- `showcase_link` - Link to personalized collection showcase
- `properties_count` - Number of properties in their collection

**Subject:** Your Personalized Property Collection - {property_address}

**Sent to:** Visitor

**Location:** `app/api/open_houses_routes.py:217`

---

### 4. `tour_request`
**Purpose:** Notify agent when visitor requests a property tour

**Variables:**
- `agent_name` - First name of the agent
- `visitor_name` - Full name of the visitor
- `visitor_email` - Visitor's email address
- `visitor_phone` - Visitor's phone number (or "Not provided")
- `property_address` - Address of the property
- `preferred_dates` - Comma-separated list of preferred tour dates/times
- `message` - Optional message from the visitor

**Subject:** New Tour Request - {property_address}

**Sent to:** Agent

**Location:** `app/services/property_tour_service.py:107`

---

### 5. `new_properties_synced`
**Purpose:** Notify both agent and visitor when new properties are automatically added to a collection

**Variables:**
- `recipient_name` - Name of recipient (agent first name or visitor name)
- `collection_name` - Name of the collection
- `new_count` - Number of new properties added
- `total_count` - Total number of properties in collection now
- `collection_link` - Link to view the collection
- `visitor_name` - (Agent emails only) Name of the visitor who owns the collection

**Subject (Visitor):** New Properties Added to Your Collection - {collection_name}

**Subject (Agent):** New Properties Added to {visitor_name}'s Collection

**Sent to:** Both visitor and agent

**Location:**
- Visitor: `app/services/property_sync_service.py:295`
- Agent: `app/services/property_sync_service.py:310`

---

### 6. `property_comment`
**Purpose:** Notify agent when visitor leaves a comment on a property

**Variables:**
- `recipient_name` - Agent's first name
- `commenter_name` - Name of the person who commented (or "A visitor")
- `property_address` - Address of the property
- `comment_text` - The actual comment content
- `collection_link` - Link to view the collection

**Subject:** New Comment on Property - {property_address}

**Sent to:** Agent

**Location:** `app/services/property_interactions_service.py:128`

---

### 7. `visitor_liked_property`
**Purpose:** Notify agent when visitor likes a property

**Variables:**
- `agent_name` - First name of the agent
- `visitor_name` - Name of the visitor (or "A visitor")
- `property_address` - Address of the property
- `collection_link` - Link to view the collection

**Subject:** A Visitor Liked a Property - {property_address}

**Sent to:** Agent

**Location:** `app/services/property_interactions_service.py:98`

---

## Implementation Notes

### EmailService Class
**Location:** `app/services/email_service.py`

**Method:** `send_simple_message(to_email, subject, template, template_variables)`

**Dev/Prod Modes:**
- When `MAILGUN_DEV=yes`: Uses sandbox email addresses from env vars
- When `MAILGUN_DEV=no`: Uses actual recipient email addresses

### Template Variable Syntax
Use `%recipient.variable_name%` in Mailgun templates (not `{{variable}}`).

Example: `%recipient.reset_link%`, `%recipient.agent_name%`

### Error Handling
All email sending is wrapped in try-catch within `EmailService.send_simple_message()`.
Failed emails return `(500, error_message)` but don't crash the application.




## How Subscriptions work
1. Signup with standard/premium, you have free trials for 30 days
2. If you upgrade or downgrade, you do not need to put any credit card details in, it does it automatically (only when you are on the free trial)
3. If you cancel during the trial, you have the remaining trial time access to keep using the same tier you had.
4. However if you resubscribe (even during the trial period you have to pay for the subscription without a trial)
5. now after resubscribing to basic, I then upgrade to premium, it does not charge the user, but the premium charges takes place on the curret billing cycle, while giving the user access to the premium features
6. If you cancel, even if the month is not over, you no longer have access to anything

Potential problem
When I upgrade, to premium from the basic (active, not trial) it does not charge me. Infinite premium after downgrade?


## How I want the next steps to look:
1. Build a PDF for similar properties (maybe)
###  Try new idea (extend the similar property radius then do the following) 
2. When client signs into open house 
    - use the description from the property visited + metadata
    - build combined description string
    - Generate embeddings and store in the vector database
    - Query top-N similar property vectors
    - Have these properties in the collection


- when a tour is confirmed there is no notification for the agent on the website no sign of a tour request incase they missed the email.  (build notification system)

- Fix the link in the tour email that says open in openhousepal or remove it

- Make the PDF better

## Can't do without email template or it's not worth the trouble
1. Agent should get email that someone signed into their open house form (not working)
4. welcome email template does not have agent name on it.
5. at the bottom of every email maybe there should an agent signaturewith phone and email and say if you have any questions or contact me here if you need. 
6. If the open house guest doesn't have an agent that an email should be sent to them 1 hr later inviting them to their personalized showcase (not sending the email right away) also if a showcase is being created for the guest there should be a (Similar Homes) Button that takes them to their showcase right away.

## I disapprove of the following:
1. also jake should be able to edit the showcase perameters actually just give them showcase admin controls so if they want to add a city thery can and they can edit it.
2. when a collection is made it says 3.3 miles (agents will see this and be like thats it? im making it 5 or 10.

If the collection fails to create at the end of the openhousesignin form, I just want to say thank you not unable to load property
