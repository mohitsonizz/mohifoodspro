from django.shortcuts import render, redirect
from django.http import HttpResponse, JsonResponse
from carts.models import CartItem
from .forms import OrderForm
import datetime
from .models import Order, Payment, OrderProduct
import json
from store.models import Product
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

# --- Add these imports for Razorpay ---
import razorpay
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt # To allow POST from Razorpay JS
# ------------------------------------


# --- Your existing payments() view (for PayPal) ---
# You might keep this for reference or remove it if only using Razorpay
def payments(request):
    try:
        body = json.loads(request.body)
        order = Order.objects.get(user=request.user, is_ordered=False, order_number=body['orderID'])

        # Store transaction details inside Payment model
        payment = Payment(
            user = request.user,
            payment_id = body['transID'],
            payment_method = body['payment_method'],
            amount_paid = order.order_total,
            status = body['status'],
        )
        payment.save()

        order.payment = payment
        order.is_ordered = True
        order.save()

        # Move the cart items to Order Product table
        cart_items = CartItem.objects.filter(user=request.user)

        for item in cart_items:
            orderproduct = OrderProduct()
            orderproduct.order_id = order.id
            orderproduct.payment = payment
            orderproduct.user_id = request.user.id
            orderproduct.product_id = item.product_id
            orderproduct.quantity = item.quantity
            orderproduct.product_price = item.product.price
            orderproduct.ordered = True
            orderproduct.save()

            
            # Reduce the quantity of the sold products
            try:
                product = Product.objects.get(id=item.product_id)
                product.stock -= item.quantity
                product.save()
            except Product.DoesNotExist:
                # Handle case where product might have been deleted
                pass # Or log an error

        # Clear cart
        CartItem.objects.filter(user=request.user).delete()

        # Send order recieved email to customer
        mail_subject = 'Thank you for your order!'
        message = render_to_string('orders/order_recieved_email.html', {
            'user': request.user,
            'order': order,
        })
        to_email = request.user.email
        send_email = EmailMessage(mail_subject, message, to=[to_email])
        send_email.send()

        # Send order number and transaction id back to sendData method via JsonResponse
        data = {
            'order_number': order.order_number,
            'transID': payment.payment_id,
        }
        return JsonResponse(data)

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Order.DoesNotExist:
        return JsonResponse({'error': 'Order not found'}, status=404)
    except Exception as e:
        # Log the error e for debugging
        print(f"Error in payments view: {e}")
        return JsonResponse({'error': 'An internal server error occurred'}, status=500)


# --- Your existing place_order() view (Correctly updated for Razorpay context) ---
def place_order(request, total=0, quantity=0,):
    current_user = request.user
    cart_items = CartItem.objects.filter(user=current_user)
    cart_count = cart_items.count()
    if cart_count <= 0:
        return redirect('store')

    grand_total = 0
    tax = 0
    total = 0 # Initialize total
    for cart_item in cart_items:
        # Ensure price and quantity are valid numbers
        price = cart_item.product.price if cart_item.product.price else 0
        qty = cart_item.quantity if cart_item.quantity else 0
        total += (price * qty)
        quantity += qty
    
    # Calculate tax (ensure total is not zero to avoid division by zero if needed)
    tax = (2 * total) / 100 if total > 0 else 0 
    grand_total = total + tax

    if request.method == 'POST':
        form = OrderForm(request.POST)
        if form.is_valid():
            # Store all the billing information inside Order table
            data = Order()
            data.user = current_user
            data.first_name = form.cleaned_data['first_name']
            data.last_name = form.cleaned_data['last_name']
            data.phone = form.cleaned_data['phone']
            data.email = form.cleaned_data['email']
            data.address_line_1 = form.cleaned_data['address_line_1']
            data.address_line_2 = form.cleaned_data.get('address_line_2', '') # Use .get for optional field
            data.country = form.cleaned_data['country']
            data.state = form.cleaned_data['state']
            data.city = form.cleaned_data['city']
            data.order_note = form.cleaned_data.get('order_note', '') # Use .get for optional field
            data.order_total = grand_total # Store original total
            data.tax = tax
            data.ip = request.META.get('REMOTE_ADDR')
            data.save()
            # Generate order number
            yr = int(datetime.date.today().strftime('%Y'))
            dt = int(datetime.date.today().strftime('%d'))
            mt = int(datetime.date.today().strftime('%m'))
            d = datetime.date(yr,mt,dt)
            current_date = d.strftime("%Y%m%d")
            order_number = current_date + str(data.id)
            data.order_number = order_number
            data.save()

            order = Order.objects.get(user=current_user, is_ordered=False, order_number=order_number)

            # Calculate amount in paisa (assuming INR)
            grand_total_paisa = int(grand_total * 100)

            context = {
                'order': order,
                'cart_items': cart_items,
                'total': total,
                'tax': tax,
                'grand_total': grand_total,
                'grand_total_paisa': grand_total_paisa, # Amount for Razorpay
                'razorpay_key_id': settings.RAZORPAY_KEY_ID, # Pass Key ID
            }
            return render(request, 'orders/payments.html', context)
        else:
            # Form invalid, re-render checkout
            print("Form Errors:", form.errors) # Log form errors for debugging
            context = {
                'form': form,
                'cart_items': cart_items,
                'total': total,
                'tax': tax,
                'grand_total': grand_total,
            }
            return render(request, 'store/checkout.html', context)
    else:
        # GET request handling
        # Redirect to checkout as GET request to place_order doesn't make sense
        return redirect('checkout')


# --- NEW: Razorpay Start Payment View ---
@csrf_exempt # Use this decorator initially, consider proper CSRF later
def start_payment(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            amount = int(data['amount']) # Amount should be in paisa
            currency = data.get('currency', 'INR')
            receipt = data.get('receipt') # Your internal order number

            # Basic validation
            if amount <= 0:
                 return JsonResponse({'error': 'Amount must be greater than zero'}, status=400)
            if not receipt:
                 return JsonResponse({'error': 'Receipt (order number) is required'}, status=400)

            client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

            DATA = {
                'amount': amount,
                'currency': currency,
                'receipt': receipt,
                'payment_capture': '1' # Auto capture payment
            }
            razorpay_order = client.order.create(data=DATA)
            print("Razorpay Order Created:", razorpay_order) # Log for debugging
            # Send back only the order ID needed by the frontend
            return JsonResponse({'order_id': razorpay_order['id']})

        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON data provided'}, status=400)
        except KeyError as e:
             print(f"Missing key in start_payment data: {str(e)}")
             return JsonResponse({'error': f'Missing key: {str(e)}'}, status=400)
        except razorpay.errors.BadRequestError as e:
            print(f"Razorpay BadRequestError in start_payment: {e}")
            # Try to provide a more specific error if possible from e.description
            error_message = f'Razorpay error: {str(e)}'
            if hasattr(e, 'description') and e.description:
                error_message = e.description
            return JsonResponse({'error': error_message}, status=400)
        except Exception as e:
            print(f"General Error in start_payment: {e}") # Log other errors
            return JsonResponse({'error': 'An internal server error occurred while initiating payment.'}, status=500)

    return JsonResponse({'error': 'Invalid request method. Only POST is allowed.'}, status=405)


# --- NEW: Razorpay Verify Payment View ---
@csrf_exempt # Use this decorator initially
def verify_payment(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            razorpay_order_id = data.get('razorpay_order_id')
            razorpay_payment_id = data.get('razorpay_payment_id')
            razorpay_signature = data.get('razorpay_signature')
            django_order_number = data.get('django_order_number') # Your internal order number

            # Basic validation
            if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature, django_order_number]):
                 return JsonResponse({'success': False, 'error': 'Missing required payment details'}, status=400)

            params_dict = {
                'razorpay_order_id': razorpay_order_id,
                'razorpay_payment_id': razorpay_payment_id,
                'razorpay_signature': razorpay_signature
            }

            client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

            # Verify the signature
            client.utility.verify_payment_signature(params_dict)
            print("Signature Verified Successfully for order:", django_order_number) # Log

            # --- Critical Section: Update Database ---
            try:
                # Find your internal order - ensure it belongs to the logged-in user and is not already paid
                order = Order.objects.get(user=request.user, is_ordered=False, order_number=django_order_number)

                # Create Payment record
                payment = Payment(
                    user=request.user,
                    payment_id=razorpay_payment_id, # Store Razorpay's ID
                    payment_method='Razorpay',
                    amount_paid=order.order_total, # Amount from your order record
                    status='Completed' # Status from signature verification success
                )
                payment.save()

                # Update Order
                order.payment = payment
                order.is_ordered = True
                order.save()

                # Move cart items to OrderProduct (Your existing logic)
                cart_items = CartItem.objects.filter(user=request.user)
                for item in cart_items:
                    orderproduct = OrderProduct()
                    orderproduct.order_id = order.id
                    orderproduct.payment = payment
                    orderproduct.user_id = request.user.id
                    orderproduct.product_id = item.product_id
                    orderproduct.quantity = item.quantity
                    orderproduct.product_price = item.product.price
                    orderproduct.ordered = True
                    orderproduct.save()

                    # Link variations
                    try:
                        cart_item_obj = CartItem.objects.get(id=item.id) # Fetch again to be safe
                        product_variation = cart_item_obj.variations.all()
                        orderproduct_saved = OrderProduct.objects.get(id=orderproduct.id)
                        orderproduct_saved.variations.set(product_variation)
                        orderproduct_saved.save()
                    except CartItem.DoesNotExist:
                        print(f"Warning: CartItem {item.id} not found during OrderProduct variation linking.")
                    
                    # Reduce stock
                    try:
                        product = Product.objects.get(id=item.product_id)
                        product.stock -= item.quantity
                        product.save()
                    except Product.DoesNotExist:
                         print(f"Warning: Product {item.product_id} not found during stock reduction.")

                # Clear cart
                CartItem.objects.filter(user=request.user).delete()

                # Send email (Your existing logic)
                mail_subject = 'Thank you for your order!'
                message = render_to_string('orders/order_recieved_email.html', {
                    'user': request.user, 'order': order,
                })
                to_email = request.user.email
                send_email = EmailMessage(mail_subject, message, to=[to_email])
                send_email.send()

                # Return success response for JavaScript redirection
                return JsonResponse({
                    'success': True,
                    'order_number': order.order_number,
                    'transID': payment.payment_id, # Use Razorpay's ID
                })

            except Order.DoesNotExist:
                 print(f"Error: Order {django_order_number} not found or already processed for user {request.user.id}.")
                 return JsonResponse({'success': False, 'error': 'Order not found or already processed'}, status=404)
            except Exception as e: # Catch potential errors during DB update
                print(f"Error updating database after payment verification: {e}")
                # Consider how to handle this - maybe log it and inform the user to contact support
                return JsonResponse({'success': False, 'error': 'Error finalizing order. Please contact support.'}, status=500)
            # --- End Critical Section ---

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON data'}, status=400)
        except razorpay.errors.SignatureVerificationError as e:
            print(f"Signature Verification Failed: {e}")
            return JsonResponse({'success': False, 'error': 'Payment signature verification failed'}, status=400)
        except Exception as e:
            print(f"General Error in verify_payment: {e}") # Log other errors
            return JsonResponse({'success': False, 'error': 'An internal server error occurred during verification'}, status=500)

    return JsonResponse({'error': 'Invalid request method. Only POST is allowed.'}, status=405)


# --- Your existing order_complete() view ---
def order_complete(request):
    order_number = request.GET.get('order_number')
    transID = request.GET.get('payment_id') # This should be the Razorpay Payment ID now

    # Basic validation
    if not order_number or not transID:
        return redirect('home') # Or show an error page

    try:
        # Ensure order belongs to the current user
        order = Order.objects.get(order_number=order_number, user=request.user, is_ordered=True)
        # Ensure payment ID matches the one associated with the order
        payment = Payment.objects.get(payment_id=transID, user=request.user)
        # Verify the payment is linked to the correct order (optional but good practice)
        if order.payment != payment:
            print(f"Warning: Payment ID {transID} does not match order {order_number}'s payment.")
            # Decide how to handle this - redirect home or show error
            return redirect('home')

        ordered_products = OrderProduct.objects.filter(order_id=order.id)

        subtotal = 0
        for i in ordered_products:
            subtotal += i.product_price * i.quantity

        context = {
            'order': order,
            'ordered_products': ordered_products,
            'order_number': order.order_number,
            'transID': payment.payment_id,
            'payment': payment,
            'subtotal': subtotal,
        }
        return render(request, 'orders/order_complete.html', context)
    except (Payment.DoesNotExist, Order.DoesNotExist):
        print(f"Order {order_number} or Payment {transID} not found for user {request.user.id}.")
        return redirect('home') # Redirect if order/payment not found for the user
    except Exception as e:
        print(f"Error in order_complete view: {e}")
        return redirect('home') # Generic error handling