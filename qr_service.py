import qrcode
import io
import base64
from models import Ticket


def generate_qr_base64(ticket: Ticket) -> str:
    """
    Generates a high-contrast QR Code containing only the server-verified token.
    Returns Base64 Data URI (e.g. data:image/png;base64,...).
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(ticket.token)
    qr.make(fit=True)

    # Prime IT Club royal blue on white keeps the code scannable at a distance.
    img = qr.make_image(fill_color="#123ABC", back_color="#ffffff")

    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{img_str}"


def generate_qr_data_uri(payload: str) -> str:
    """Render a one-time MFA provisioning URI as a PNG data URI."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#123ABC", back_color="#ffffff")
    buffered = io.BytesIO()
    image.save(buffered, format="PNG")
    encoded = base64.b64encode(buffered.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
