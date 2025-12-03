import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication


# FROM_EMAIL = "noreply@tronlab.cn"
# SMTP_SERVER = "smtp.mxhichina.com"
# SMTP_PORT = 587
# SMTP_PASSWORD = "Tron!@#246"

class SendMail:
    def __init__(self, smtp_password):
        self.FROM_EMAIL = "noreply100@tron.network"
        self.SMTP_SERVER = "smtp.gmail.com"
        self.SMTP_PORT = 587
        self.SMTP_PASSWORD = smtp_password


    def send_text(self, title, content, to_email):
        msg = MIMEText(content, 'plain', 'utf-8')
        msg['Subject'] = title
        msg["Accept-Language"] = "zh-CN"
        msg["Accept-Charset"] = "ISO-8859-1,utf-8"
        self.send_email(to_email, msg)


    def send_html(self,title, content, to_email):
        msg = MIMEText(content, 'html', 'utf-8')
        msg['Subject'] = title
        msg["Accept-Language"] = "zh-CN"
        msg["Accept-Charset"] = "ISO-8859-1,utf-8"
        self.send_email(to_email, msg)


    def send_attach(self, subject, body, attachment, name, to_email):
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        part = MIMEApplication(attachment.getvalue(), Name='name')
        part['Content-Disposition'] = f'attachment; filename="{name}"'
        msg.attach(part)
        self.send_email(to_email, msg)


    def send_email_with_attachment(self,to_email, cc_email,  subject, body, attachment, attachment_name):
        try:
            msg = MIMEMultipart()
            msg['From'] = self.FROM_EMAIL
            msg['To'] = ", ".join(to_email)
            msg['Cc'] = ", ".join(cc_email)
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            part = MIMEApplication(attachment.getvalue(), Name=attachment_name)
            part['Content-Disposition'] = f'attachment; filename="{attachment_name}"'
            msg.attach(part)

            all_recipients = to_email + cc_email

            with smtplib.SMTP(self.SMTP_SERVER, self.SMTP_PORT) as server:
                server.starttls()
                server.login(self.FROM_EMAIL, self.SMTP_PASSWORD)
                server.sendmail(self.FROM_EMAIL, all_recipients, msg.as_string())
                print("Email sent successfully！")

        except Exception as e:
            print(f"Mail sending failure: {e}")


    def send_email(self,to_email, msg, logger=None):
        msg['From'] = self.FROM_EMAIL
        msg['To'] = ", ".join(to_email)
        try:
            server = smtplib.SMTP(self.SMTP_SERVER, self.SMTP_PORT)
            server.starttls()
            server.login(self.FROM_EMAIL, self.SMTP_PASSWORD)
            text = msg.as_string()
            server.sendmail(self.FROM_EMAIL, to_email, text)
            server.quit()
            msg = "Email sent successfully"
            if logger:
                logger.info(msg)
        except Exception as e:
            msg = f"Mail sending failure: {e}"
            if logger:
                logger.info(msg)
