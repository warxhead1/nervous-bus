class InsufficientFunds(Exception):
    pass


class Account:
    def __init__(self, name, balance=0):
        self.name = name
        self.balance = balance

    def deposit(self, cents):
        if cents <= 0:
            raise ValueError("deposit must be positive")
        self.balance += cents

    def withdraw(self, cents):
        """Take cents out; the balance may reach zero but never go below it."""
        if cents <= 0:
            raise ValueError("withdrawal must be positive")
        if cents > self.balance:
            raise InsufficientFunds(self.name)
        self.balance -= cents


def transfer(src, dst, cents):
    """Move cents from src to dst. On failure neither account changes."""
    src.withdraw(cents)
    dst.deposit(cents)
