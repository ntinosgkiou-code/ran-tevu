import os

def send_sms(to_phone: str, message: str) -> bool:
    sid   = os.environ.get('TWILIO_ACCOUNT_SID')
    token = os.environ.get('TWILIO_AUTH_TOKEN')
    # ίδιο νούμερο όπου κι αν οριστεί (τα διάφορα paths χρησιμοποιούσαν διαφορετικά ονόματα)
    from_ = (os.environ.get('TWILIO_DEFAULT_FROM') or os.environ.get('TWILIO_PHONE_NUMBER')
             or os.environ.get('TWILIO_FROM_NUMBER'))
    if not (sid and token and from_):
        print('[SMS] Twilio not configured — skipping')
        return False
    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(body=message, from_=from_, to=to_phone)
        print(f'[SMS] Sent to {to_phone}')
        return True
    except Exception as e:
        # Twilio βάζει το πραγματικό «γιατί» (invalid number, unverified sender, no funds) στο error
        try:
            from debug_log import log_error
            log_error(f"send_sms(to={to_phone}, from={from_})", e)
        except Exception:
            print(f'[SMS ERROR] {type(e).__name__}: {e}')
        return False

def sms_reminder(booking: dict, hours_before: int) -> bool:
    service = booking.get('service', '')
    time_   = booking.get('time', '')
    phone   = booking.get('phone', '')
    if hours_before == 24:
        msg = f'Υπενθύμιση: Αύριο {time_} έχετε ραντεβού για {service}. Αν θέλετε να ακυρώσετε, επικοινωνήστε μαζί μας.'
    else:
        msg = f'Υπενθύμιση: Σήμερα στις {time_} έχετε ραντεβού για {service}. Σας περιμένουμε!'
    return send_sms(phone, msg)
