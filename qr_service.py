import qrcode
import io
import base64
import json
from models import Ticket


def generate_qr_base64(ticket: Ticket) -> str:
    """
    Generates a high-contrast digital QR Code containing ticket token and metadata.
    Returns Base64 Data URI (e.g. data:image/png;base64,...).
    """
    payload = json.dumps({
        "ticket_id": ticket.ticket_id,
        "token": ticket.token,
        "event_id": ticket.event_id,
        "name": ticket.student_name,
        "college_id": ticket.college_id
    })

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    # Prime IT Club royal blue on white keeps the code scannable at a distance.
    img = qr.make_image(fill_color="#123ABC", back_color="#ffffff")

    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{img_str}"
