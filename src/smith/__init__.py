"""Smith: read-only personal wealth adviser scaffold."""
import decimal

# One Decimal policy for every path: calculation, rounding, serialization and display. Imported values
# have at most 15 integer and 8 fractional digits, so amount x rate and quantity x price x rate need
# about 70 digits; the default 28-digit context would raise InvalidOperation when quantizing such a
# product. Set here, on package import, so no module can run under the default (threads included).
DECIMAL_PRECISION = 100
decimal.DefaultContext.prec = DECIMAL_PRECISION
decimal.getcontext().prec = DECIMAL_PRECISION
