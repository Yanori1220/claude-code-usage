import type { Register } from 'claude-code'

// Writes the account's limit windows (five_hour, seven_day) to $CC_USAGE_DIR/ratelimits.json
// (default ~/cc-usage), where cc_usage.py reads them. The readings are the account's, so
// whichever session measured last holds the newest ones.
export const register: Register = on => {
  on('session.measure', async ($, e, next) => {
    if (e.changed.includes('rateLimits') && e.rateLimits.length > 0) {
      const dir = (await $.env.get('CC_USAGE_DIR')) || `${await $.env.get('HOME')}/cc-usage`
      const at = await $.clock.now()
      await $.fs.write(`${dir}/ratelimits.json`, JSON.stringify({ at, rateLimits: e.rateLimits }) + '\n')
    }
    return next(e)
  })
}
