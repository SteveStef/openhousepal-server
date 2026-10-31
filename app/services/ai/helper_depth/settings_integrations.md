Settings and connections in depth: every Settings tab, which emails you and your clients get (and which can't be turned off), sending from your own Gmail, Google Calendar sync, CRM (Follow Up Boss, other CRMs, webhook, portal leads in), Help tickets, and the Account setup checklist with its required steps.

## Settings (gear icon; phone: Account → Settings; "Your account, your way")
Tabs: **Profile**, **Notifications**, **Tools**, **IDX Website** (IDX only), **Billing**, **Account**. Changes save on their own.

**Profile**
- Profile photo (**Upload photo** / **Change photo**, drag to reposition). **Full Name**, **Title**, **Brokerage** (required), **Team**.
- **About**: up to 280 characters / 3 lines so the Brag Sheet fits one page. **Polish with AI** (tones: Professional, Warm, Luxury, Energetic, Concise; uses a little AI allowance). Compliance hints show under the box.
- Contact: **Mobile Number (Call)**, **Text Phone Number**, **Contact Email** (required), **Office Phone (optional)** (goes on your Save Contact card), **Your Brokerage's Phone** (required - printed on designs for compliance), **Market** (required), **License Number** (locked once saved; **Not set - add it** opens setup).
- Social Links, **My Listings**, **Where Your Listings Link To**, **Recent Sales** (same panels as the Agent Hub editor - editing in either place updates both).
- **The middle of your QR codes**: **Our house logo** (every plan); **Your logo** or **Your initials** (IDX only; logo needs a Brand Kit logo). Printed codes keep working.

**Notifications** - which alerts also email you (the in-app bell always shows everything). Each row has an Email switch:
- New leads & sign-ins, Client requests in Showcases, Showcase activity, Daily briefing, Spotlight weekly report (Mondays), Liked homes (hourly), Open house host requests, Team & broadcasts, My listing updates (off by default), AI media ready (off by default), New reviews, Account & support. Phone push isn't available.

**Tools** - quick status of **Calendar**, **Reviews** and **CRM**, each with **Manage** or **Open & Set Up**.

**IDX Website** (IDX) - the website on/off switch, Your Website status, **View My Website** / **Edit My Website**, **IDX Domain** (free address, your own domain, **Use my own domain for my links**), **More website settings**.

**Billing** - Current Plan, status (Free Trial, Active, Cancelled, Payment Failed, Expired; or Managed / Brokerage Paid), dates (Trial Ends, Active Since, Last Billing, First Payment / Next Renewal, Access Ends), **View full plan details**, **Plan Options** ("Upgrade or Modify"), **Manage Subscription** → **Cancel Plan**, **Restore Access**, **Update payment method on PayPal**, **Start Fresh**, **Resubscribe**. The Monthly AI Usage and storage meters also show in Settings.

**Account** - **Password** (Current, New - at least 8 characters, Confirm; or **Reset it by email**). Account details: Sign-in email, MLS ID (or License number), State, Plan, Status, Member since. Changing sign-in email, MLS ID or license = **Contact support**. Shortcuts: Your team →, Agent Hub → (IDX: Website & domain →), Billing →.

## Tools → Email (what goes out, from which address)
"These settings only ever send less than our standard - never more."
- **Send from your own email**: **Connect Gmail** → sign in on the new tab (allow the send permission). Client emails then come from your real address and show in your Sent folder. **Send me a test** checks it. **Send my guest emails from this address** switches it on/off. Outlook and Microsoft 365 show "Coming soon".
  - If sending fails (password change, removed permission, or an office account that blocks apps), it shows "Needs reconnecting" and guest emails go out from OpenHousePal meanwhile - nothing is lost. Office (Google Workspace) accounts may need IT to allow OpenHousePal.
  - Your own alerts always come from OpenHousePal.
- **What your clients get** (each row: on/off and **Sends from** Your email / OpenHousePal):
  - Most important: **Showcase invite after your open house** (**Goes out**: Right away, 15 min after, 30 min after, **1 hour after** - default), **Showcases you make for a client**, **Showcase link when someone makes their own**, **Tour confirmations**, **Your replies to their comments**.
  - Automatic updates: **New homes & price drops in their Showcase** (**How often**: Up to daily - one 4:30 PM email on days something changed - or Once a week), **Listing Spotlight alerts**.
  - Off = never goes out. No-MLS plans only see **Tour confirmations** (from the Schedule a Showing form).
- **What lands in your inbox**: grouped "Most important" (New leads & sign-ins, Client requests in Showcases, Open house host requests), "Good to know" (Daily briefing, Liked homes, Showcase activity, New reviews, Spotlight weekly report, Team & broadcasts), "Extras" (My listing updates, AI media ready). No-MLS plans don't see the Showcase, hosting, briefing, liked-homes, Spotlight, listing and team rows.
- **Daily briefing → Arrives at**: every 15 minutes from 6:00 AM to 9:00 PM Eastern; default 4:30 PM. Only shows while Daily briefing is on.
- Can't be turned off: sign-in codes and password resets, billing and plan notices, support replies and connection problems.

## Calendar (Tools → Calendar, "Your follow-ups, on your calendar"; every plan)
- **Google Calendar** → **Connect Google** → sign in ("If Google says 'unverified app': Advanced → Go to OpenHousePal → Allow") → you land back synced.
- Creates one calendar called "OpenHousePal" - it never touches your other calendars. Shows follow-ups (with your buzz settings), scheduled open houses and approved hosting dates, and confirmed Showcase tours. Two-way: edit events in Google, and new follow-ups skip times you're already booked.
- **Sync now** pushes everything again. **Disconnect** removes the OpenHousePal calendar and its events from Google. **Sign-in expired** → **Reconnect**.
- **Outlook Calendar**: "Coming soon".
- Times use Eastern time.

## CRM (Tools → CRM, "Every lead, where it belongs"; every plan)
Two separate switches - set up either or both.
- **Leads OUT** - every new OpenHousePal lead goes to your CRM:
  1. **Which CRM do you use?**: Follow Up Boss, Lofty (formerly Chime), kvCORE / BoldTrail, Sierra Interactive, BoomTown, Real Geeks, Wise Agent, LionDesk, IXACT Contact, Top Producer, Another CRM, Webhook / Zapier. Each shows exactly where to click in that CRM.
  2. **Connect it**: Follow Up Boss → paste the **Follow Up Boss API key** ("Best option"; Admin → API → Create API Key; copy it right away - FUB shows it once). Leads arrive as proper FUB events. Or use the FUB lead email. Other CRMs → paste their lead-parsing email ("We email each new lead here in the format your CRM already reads"). Real Geeks and others without one → **Use a webhook / Zapier instead** ("We send name, email, phone, source and message for each lead").
  3. **Save & send a test lead**, check your CRM. Make sure sending is on ("Saved - switched off" means real leads won't go yet). A wrong FUB key shows **Key rejected**.
  - A FUB key from a non-admin team member still creates leads, but they follow the team's lead routing.
- **Leads IN** - portal leads: copy **Your private forwarding address** and follow **How to forward from each portal** (Zillow, Realtor.com, Homes.com, Facebook). Forwarded lead emails appear in Leads tagged "Portal Lead" and trigger the Listing inquiry follow-up. Generating a new address stops the old one immediately.
- The Leads page shows a CRM chip (connected / Connect CRM).

## Help tickets (question-mark icon; phone: Account → Help; "We're here to help")
- **Submit a Ticket** → pick **Contact support** ("What do you need help with?", "Tell us more", **How urgent is it?**: Whenever / Soon / Urgent), **Report a bug** ("Sum it up in a few words", "What happened?", "What did you expect to happen?", **Where did it happen?**: Dashboard, Open Houses, Showcases, Leads, CMA, Design Studio, Spotlight, IDX Website, Agent Hub, Discovery, Settings & billing, Something else) or **Suggest a feature** ("Your idea in a few words", "What would you like?", "How would it help you?").
- Up to 3 screenshots. Buttons: **Send to support** / **Send bug report** / **Send my idea**. You get a Reference #.
- A real person replies on the page; you get a notification and the Help icon shows a red dot. **Type a reply...**. Tabs Open / Closed; replying to a closed ticket reopens it. **Close Ticket** / **Reopen**.
- CMA problems: use **Submit Ticket** inside the CMA so the Report Code is attached.

## Account setup checklist (clipboard icon; phone: Account → Account setup)
- Saves as you type and remembers where you left off. **Continue**, **Back**, **Skip** (each optional step says what skipping means), **Finish** on the last step.
- Groups and steps:
  - **About you**: About you, How to reach you, License number (these three are **Required** and can't be skipped), Your photo, Your story, Your socials.
  - **Your pages**: Your sales, Reviews, Your brand, Agent Hub, Your forms, Email signature.
  - **Your open houses**: Open house defaults (kit defaults and the Showcase invite to guests without an agent).
  - **Staying in touch**: Alerts & follow-ups, Calendar, Your email, Your CRM.
  - **Your website** (called **Your towns** below IDX): Towns you serve, Website homes, Your website, Web address.
- Which steps you see: every plan - About you, How to reach you, License number, Your photo, Your brand, Open house defaults, Alerts & follow-ups, Calendar, Your email, Your CRM, Towns you serve. Agent Hub plans add Your story, Your socials, Your sales, Agent Hub, Your forms. No-MLS Basic adds Your sales. Review plans add Reviews. IDX and No-MLS Premium add Email signature. IDX adds Website homes, Your website, Web address.
- "I don't use this" choices remove a step for good (undo any time): **I don't need a calendar**, **Keep sending from OpenHousePal**, **I don't use a CRM**.
- Status chips: **Done**, **Skipped**, **Not using**, **Required**; after an upgrade new steps say **New for your plan**.
- Waiting on broker approval: the About you steps can be done early.
- Finish shows shortcuts: **Open my Agent Hub**, **My email signature** (IDX, No-MLS Premium), **My website** (IDX).
- Settings shows a red "!" while a required item is missing.

## Can't do / common mistakes
- Can't change sign-in email, MLS ID or a saved license number yourself - Contact support.
- Can't send from Outlook or sync an Outlook calendar yet.
- Turning off a client email row stops it entirely; personal emails like tour confirmations only send when that row is on.
- Calendar alerts need a connected calendar; without one, follow-ups live only in OpenHousePal.
