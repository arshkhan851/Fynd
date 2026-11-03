import abc
import logging

logger = logging.getLogger(__name__)

class BaseEmailProvider(abc.ABC):
    """Abstract interface for all email dispatchers."""
    
    @abc.abstractmethod
    def send_magic_link(self, email: str, raw_token: str) -> None:
        """Sends a magic link containing the raw token to the specified email."""
        pass


class ConsoleEmailProvider(BaseEmailProvider):
    """Safe test implementation that prints the email contents to the terminal."""
    
    def __init__(self, frontend_url: str = "http://localhost:3000"):
        self.frontend_url = frontend_url

    def send_magic_link(self, email: str, raw_token: str) -> None:
        # The token is passed via a URL fragment (#) or query parameter. 
        # Using a query parameter here for standard POST exchange in the frontend.
        magic_link = f"{self.frontend_url}/auth/verify?token={raw_token}"
        
        print("\n" + "=" * 60)
        print(f"📧 EMAIL INTERCEPTED (LOCAL DEV)")
        print(f"To: {email}")
        print(f"Subject: Log in to Fynd")
        print(f"Body: Click the link below to securely log in.")
        print(f"🔗 Link: {magic_link}")
        print("=" * 60 + "\n")
        
        logger.info("Mock magic link generated for %s", email)


# Factory function to be used as a FastAPI dependency or service locator
def get_email_provider() -> BaseEmailProvider:
    # Future enhancement: Check environment variables here (e.g., EMAIL_PROVIDER=sendgrid)
    # and return the appropriate production implementation.
    return ConsoleEmailProvider()
