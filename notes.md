<div id="paypal-button-container-P-4KN61644XJ8589200NDUAG6A"></div>
<script src="https://www.paypal.com/sdk/js?client-id=ASpVhylrQzWRIYKocjJT2Iq9CRRS5OOrlJrQwbu78XvNMV8XDvR62Vdq-nFAbqnN9N8cDiz5rKNEcrka&vault=true&intent=subscription" data-sdk-integration-source="button-factory"></script>
<script>
  paypal.Buttons({
      style: {
          shape: 'pill',
          color: 'gold',
          layout: 'vertical',
          label: 'subscribe'
      },
      createSubscription: function(data, actions) {
        return actions.subscription.create({
          /* Creates the subscription */
          plan_id: 'P-4KN61644XJ8589200NDUAG6A'
        });
      },
      onApprove: function(data, actions) {
        alert(data.subscriptionID); // You can add optional success message for the subscriber here
      }
  }).render('#paypal-button-container-P-4KN61644XJ8589200NDUAG6A'); // Renders the PayPal button
</script>
<div id="paypal-button-container-P-50796747YK1489053NDT7TXQ"></div>
<script src="https://www.paypal.com/sdk/js?client-id=ASpVhylrQzWRIYKocjJT2Iq9CRRS5OOrlJrQwbu78XvNMV8XDvR62Vdq-nFAbqnN9N8cDiz5rKNEcrka&vault=true&intent=subscription" data-sdk-integration-source="button-factory"></script>
<script>
  paypal.Buttons({
      style: {
          shape: 'pill',
          color: 'gold',
          layout: 'vertical',
          label: 'subscribe'
      },
      createSubscription: function(data, actions) {
        return actions.subscription.create({
          /* Creates the subscription */
          plan_id: 'P-50796747YK1489053NDT7TXQ'
        });
      },
      onApprove: function(data, actions) {
        alert(data.subscriptionID); // You can add optional success message for the subscriber here
      }
  }).render('#paypal-button-container-P-50796747YK1489053NDT7TXQ'); // Renders the PayPal button
</script>
