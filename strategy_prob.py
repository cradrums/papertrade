"""Probability strategy: estimates P(price is higher in N days) from overbought/oversold
and trend features, using a logistic regression that is retrained once a month on past
data only. The position size scales with that probability.

Same interface as strategy.py: target_weight(closes) -> 0.0..1.0, or None for "do nothing".
Needs only pandas and numpy (already installed with the bot)."""
import numpy as np
import pandas as pd


def _rsi(c, n):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def features(c):
    """Overbought/oversold and trend features, each using only data up to that bar."""
    f = pd.DataFrame({
        "rsi2": _rsi(c, 2) / 100 - 0.5,                      # short-term overbought/oversold
        "rsi14": _rsi(c, 14) / 100 - 0.5,
        "z20": (c - c.rolling(20).mean()) / c.rolling(20).std(),  # position in Bollinger band
        "d50": c / c.rolling(50).mean() - 1,                 # distance from trend averages
        "d200": c / c.rolling(200).mean() - 1,
        "mom20": c.pct_change(20),
        "vol20": c.pct_change().rolling(20).std(),
    })
    return f.replace([np.inf, -np.inf], np.nan)


def _fit(X, y, l2, iters=30):
    """Ridge-regularised logistic regression (Newton's method)."""
    Xb = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(Xb.shape[1])
    R = l2 * np.eye(len(w))
    R[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        g = Xb.T @ (p - y) + R @ w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + R + 1e-6 * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-7:
            break
    return w


def _months(closes):
    try:
        i = pd.DatetimeIndex(closes.index)
        return (i.year * 12 + i.month).to_numpy()
    except Exception:
        return np.arange(len(closes)) // 21


class ProbStrategy:
    def __init__(self, horizon=5, train_window=1000, min_train=500,
                 p_low=0.50, p_high=0.62, step=0.25, l2=10.0, smooth=10):
        assert p_low < p_high
        self.h, self.window, self.min_train = int(horizon), int(train_window), int(min_train)
        self.p_low, self.p_high, self.step, self.l2 = p_low, p_high, step, l2
        self.smooth = max(1, int(smooth))  # average the probability over the last N bars to cut churn
        self._month, self._model, self.last_p = None, None, None

    def _train(self, c):
        f = features(c)
        fwd = c.shift(-self.h)
        ok = f.notna().all(axis=1) & fwd.notna()   # drops the last h bars: their labels aren't known
        f, y = f[ok].iloc[-self.window:], (fwd > c)[ok].astype(float).iloc[-self.window:]
        if len(f) < self.min_train or y.nunique() < 2:
            return None
        mu, sd = f.mean(), f.std()
        sd = sd.where(sd > 0, 1.0)
        return mu, sd, _fit(((f - mu) / sd).to_numpy(), y.to_numpy(), self.l2)

    def target_weight(self, closes: pd.Series):
        """Uses only data up to and including the latest close."""
        if len(closes) < 260:
            return None
        m = _months(closes)
        if m[-1] != self._month:  # new month: retrain on data from before this month only
            self._month = m[-1]
            past = closes[m < m[-1]].iloc[-(self.window + self.h + 300):]
            self._model = self._train(past)
        if self._model is None:
            return None
        mu, sd, w = self._model
        x = features(closes.iloc[-(400 + self.smooth):]).iloc[-self.smooth:]
        if x.isna().any().any():
            return None
        z = ((x - mu) / sd).to_numpy()
        p = float((1 / (1 + np.exp(-np.clip(w[0] + z @ w[1:], -30, 30)))).mean())
        self.last_p = p
        frac = min(1.0, max(0.0, (p - self.p_low) / (self.p_high - self.p_low)))
        return round(frac / self.step) * self.step
