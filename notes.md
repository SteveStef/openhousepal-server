## How Subscriptions work
1. Signup with standard/premium, you have free trials for 30 days
2. If you upgrade or downgrade, you do not need to put any credit card details in, it does it automatically (only when you are on the free trial)
3. If you cancel during the trial, you have the remaining trial time access to keep using the same tier you had.
4. However if you resubscribe (even during the trial period you have to pay for the subscription without a trial)
5. now after resubscribing to basic, I then upgrade to premium, it does not charge the user, but the premium charges takes place on the curret billing cycle, while giving the user access to the premium features
6. If you cancel, even if the month is not over, you no longer have access to anything

API restrictions
Maps JavaScript API
Maps Embed API
Places API

Billing subscription activated, 
Billing subscription cancelled, 
Billing subscription created, 
Billing subscription expired, 
Billing subscription payment failed, 
Billing subscription re-activated, 
Billing subscription suspended, 
Payment sale completed, 
Payment sale denied, 
Billing subscription updated


Mount Type
BIND
Host Path
/var/log/openhousepal_server
Mount Path
/app/logs


  1. Registration & Onboarding
   * New User: Basic Plan
       * Sign up with a new email, select Basic, and complete PayPal checkout.
       * Expect: subscription_status = TRIAL, trial_ends_at = 14/30 days out.
   * New User: Premium Plan
       * Sign up with a new email, select Premium, and complete PayPal checkout.
       * Expect: subscription_status = TRIAL, plan_tier = PREMIUM.
   * Bundle Code Signup
       * Apply a bundle code during registration (generate one with python server/manage_bundle_codes.py add TESTCODE).
       * Expect: PREMIUM tier with an extended trial (e.g., 1 year) and no immediate charge.
   * Signup Aborted
       * Close the PayPal window or click "Cancel" before completing payment.
       * Expect: No user account is created (the process is atomic).
   * Signup without Verification
       * Try to call the signup API directly without verifying the email code first.
       * Expect: 400 Bad Request (Email not verified).

  2. Plan Management
   * Upgrade during Trial (Basic → Premium)
       * While in the 14-day trial, click "Upgrade" in Settings.
       * Expect: plan_tier becomes PREMIUM; trial_ends_at remains the same.
   * Downgrade during Trial (Premium → Basic)
       * While in trial, downgrade to Basic.
       * Expect: plan_tier becomes BASIC.
   * Upgrade/Downgrade after Trial (Paid)
       * Perform plan changes once the account is in ACTIVE status.
       * Expect: PayPal revision approval flow; tier updates immediately or upon approval.

  3. Cancellation & Retention
   * Manual Cancellation
       * Click "Cancel Plan" in Settings.
       * Expect: subscription_status becomes CANCELLED.
   * Grace Period Access
       * Log in as a CANCELLED user before your next_billing_date.
       * Expect: Full access to all features (verified in lib/auth.ts:hasValidSubscription).
   * Term Expiration (Access Cutoff)
       * Attempt to access features after the grace period ends.
       * Verify: Redirect to /upgrade-required. (Simulate by setting next_billing_date to the past in DB).
   * Resubscribe (Returning User)
       * A CANCELLED or EXPIRED user clicks "Resubscribe".
       * Expect: Redirect to PayPal for a new agreement; status returns to ACTIVE.
   * Reactivate (Suspended User)
       * A user with a failed payment (SUSPENDED) clicks "Reactivate".
       * Expect: PayPal activation call; status returns to ACTIVE.

  4. System & Webhooks
   * Payment Success
       * Simulate a recurring payment (PAYMENT.SALE.COMPLETED webhook).
       * Expect: last_billing_date updates to today.
   * Payment Failure
       * Simulate a failed payment (PAYMENT.SALE.DENIED or BILLING.SUBSCRIPTION.SUSPENDED webhook).
       * Expect: subscription_status becomes SUSPENDED.
   * Subscription Hijacking
       * Try to use a subscription_id that is already linked to another account for a new signup.
       * Expect: 400 Bad Request (Subscription already linked).

Also if payment fails -> you can reactivate or do cancel and start fresh (needs testing though)
try every type of failure card: https://developer.paypal.com/tools/sandbox/card-testing/

The case where the webhook does not send to the server, when the user logins in it will update all that users subscriptions stuff
