//! Exact comparison for bounded JSON decimal text.
//! The selector only needs positivity and bid/ask ordering, not arithmetic.

use std::cmp::Ordering;

#[derive(Clone, Debug, Eq, PartialEq)]
struct SignedInteger {
    sign: i8,
    digits: Vec<u8>,
}

impl SignedInteger {
    fn zero() -> Self {
        Self {
            sign: 0,
            digits: vec![0],
        }
    }

    fn from_digits(text: &str, negative: bool) -> Option<Self> {
        let digits = digit_sequence(text)?;
        let first = digits.iter().position(|digit| *digit != 0);
        let Some(first) = first else {
            return Some(Self::zero());
        };
        Some(Self {
            sign: if negative { -1 } else { 1 },
            digits: digits[first..].to_vec(),
        })
    }

    fn add_small(mut self, value: i64) -> Self {
        let other = Self::from_i64(value);
        if other.sign == 0 {
            return self;
        }
        if self.sign == 0 {
            return other;
        }
        if self.sign == other.sign {
            self.digits = add_magnitude(&self.digits, &other.digits);
            return self;
        }
        match magnitude_order(&self.digits, &other.digits) {
            Ordering::Equal => Self::zero(),
            Ordering::Greater => {
                self.digits = subtract_magnitude(&self.digits, &other.digits);
                self
            }
            Ordering::Less => Self {
                sign: other.sign,
                digits: subtract_magnitude(&other.digits, &self.digits),
            },
        }
    }

    fn from_i64(value: i64) -> Self {
        if value == 0 {
            return Self::zero();
        }
        let mut magnitude = value.unsigned_abs();
        let mut digits = Vec::new();
        while magnitude > 0 {
            digits.push((magnitude % 10) as u8);
            magnitude /= 10;
        }
        digits.reverse();
        Self {
            sign: if value < 0 { -1 } else { 1 },
            digits,
        }
    }

    fn compare(&self, other: &Self) -> Ordering {
        match self.sign.cmp(&other.sign) {
            Ordering::Equal if self.sign == 0 => Ordering::Equal,
            Ordering::Equal if self.sign > 0 => magnitude_order(&self.digits, &other.digits),
            Ordering::Equal => magnitude_order(&other.digits, &self.digits),
            order => order,
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct PositiveDecimal {
    order: SignedInteger,
    digits: Vec<u8>,
}

impl PositiveDecimal {
    pub(crate) fn parse(text: &str) -> Option<Self> {
        let text = text.trim_matches(|character: char| character.is_ascii_whitespace());
        let (negative, body) = match text.as_bytes().first()? {
            b'+' => (false, &text[1..]),
            b'-' => (true, &text[1..]),
            _ => (false, text),
        };
        if negative {
            return None;
        }

        let (mantissa, exponent_text) = split_exponent(body)?;
        let (exponent, exponent_negative) = parse_exponent(exponent_text)?;
        let (digits, fractional_digits) = parse_mantissa(mantissa)?;
        let first = digits.iter().position(|digit| *digit != 0)?;
        let significant = digits[first..].to_vec();
        let offset =
            i64::try_from(significant.len()).ok()? - i64::try_from(fractional_digits).ok()?;
        let order = SignedInteger::from_digits(exponent, exponent_negative)?.add_small(offset);
        Some(Self {
            order,
            digits: significant,
        })
    }

    pub(crate) fn compare(&self, other: &Self) -> Ordering {
        match self.order.compare(&other.order) {
            Ordering::Equal => compare_significands(&self.digits, &other.digits),
            order => order,
        }
    }
}

fn split_exponent(text: &str) -> Option<(&str, &str)> {
    let mut positions = text.match_indices(|character| character == 'e' || character == 'E');
    let Some((index, _)) = positions.next() else {
        return Some((text, "0"));
    };
    if positions.next().is_some() {
        return None;
    }
    Some((&text[..index], &text[index + 1..]))
}

fn parse_exponent(text: &str) -> Option<(&str, bool)> {
    match text.as_bytes().first()? {
        b'+' => Some((&text[1..], false)),
        b'-' => Some((&text[1..], true)),
        _ => Some((text, false)),
    }
}

fn parse_mantissa(text: &str) -> Option<(Vec<u8>, usize)> {
    let mut digits = Vec::new();
    let mut fractional_digits = 0;
    let mut after_point = false;
    let mut previous_digit = false;
    let bytes = text.as_bytes();
    for (index, byte) in bytes.iter().copied().enumerate() {
        match byte {
            b'0'..=b'9' => {
                digits.push(byte - b'0');
                if after_point {
                    fractional_digits += 1;
                }
                previous_digit = true;
            }
            b'.' if !after_point => {
                after_point = true;
                previous_digit = false;
            }
            b'_' if previous_digit && bytes.get(index + 1).is_some_and(u8::is_ascii_digit) => {
                previous_digit = false
            }
            _ => return None,
        }
    }
    if digits.is_empty() || text.ends_with('_') {
        return None;
    }
    Some((digits, fractional_digits))
}

fn digit_sequence(text: &str) -> Option<Vec<u8>> {
    let mut digits = Vec::new();
    let mut previous_digit = false;
    let bytes = text.as_bytes();
    for (index, byte) in bytes.iter().copied().enumerate() {
        match byte {
            b'0'..=b'9' => {
                digits.push(byte - b'0');
                previous_digit = true;
            }
            b'_' if previous_digit && bytes.get(index + 1).is_some_and(u8::is_ascii_digit) => {
                previous_digit = false
            }
            _ => return None,
        }
    }
    if digits.is_empty() || !previous_digit {
        return None;
    }
    Some(digits)
}

fn compare_significands(left: &[u8], right: &[u8]) -> Ordering {
    let width = left.len().max(right.len());
    (0..width)
        .map(|index| {
            left.get(index)
                .copied()
                .unwrap_or(0)
                .cmp(&right.get(index).copied().unwrap_or(0))
        })
        .find(|order| *order != Ordering::Equal)
        .unwrap_or(Ordering::Equal)
}

fn magnitude_order(left: &[u8], right: &[u8]) -> Ordering {
    left.len().cmp(&right.len()).then_with(|| left.cmp(right))
}

fn add_magnitude(left: &[u8], right: &[u8]) -> Vec<u8> {
    let mut result = Vec::with_capacity(left.len().max(right.len()) + 1);
    let mut carry = 0;
    for offset in 0..left.len().max(right.len()) {
        let a = left.len().checked_sub(offset + 1).map_or(0, |i| left[i]);
        let b = right.len().checked_sub(offset + 1).map_or(0, |i| right[i]);
        let sum = a + b + carry;
        result.push(sum % 10);
        carry = sum / 10;
    }
    if carry > 0 {
        result.push(carry);
    }
    result.reverse();
    result
}

fn subtract_magnitude(left: &[u8], right: &[u8]) -> Vec<u8> {
    let mut result = Vec::with_capacity(left.len());
    let mut borrow = 0_i16;
    for offset in 0..left.len() {
        let a = i16::from(left[left.len() - offset - 1]) - borrow;
        let b = right
            .len()
            .checked_sub(offset + 1)
            .map_or(0, |index| i16::from(right[index]));
        let difference = if a < b {
            borrow = 1;
            a + 10 - b
        } else {
            borrow = 0;
            a - b
        };
        result.push(difference as u8);
    }
    while result.len() > 1 && result.last() == Some(&0) {
        result.pop();
    }
    result.reverse();
    result
}

#[cfg(test)]
mod tests {
    use super::PositiveDecimal;
    use std::cmp::Ordering;

    fn order(left: &str, right: &str) -> Ordering {
        PositiveDecimal::parse(left)
            .unwrap()
            .compare(&PositiveDecimal::parse(right).unwrap())
    }

    #[test]
    fn accepts_python_decimal_precision_and_exponents() {
        assert_eq!(
            order("0.00000000000000000000000000001", "1e-28"),
            Ordering::Less
        );
        assert_eq!(
            order(
                "1.00000000000000000000000000001",
                "1.00000000000000000000000000002"
            ),
            Ordering::Less
        );
        assert_eq!(order("1.20", "1.2"), Ordering::Equal);
        assert_eq!(order("1_000.00", "1e3"), Ordering::Equal);
        assert_eq!(order("1e10000", "9e9999"), Ordering::Greater);
        assert_eq!(order("1e-10000", "9e-10001"), Ordering::Greater);
    }

    #[test]
    fn rejects_nonpositive_and_malformed_decimal_text() {
        for text in ["0", "-0.1", "-0", "NaN", "Infinity", ".", "1__0", "1e+"] {
            assert!(PositiveDecimal::parse(text).is_none(), "accepted {text:?}");
        }
    }
}
