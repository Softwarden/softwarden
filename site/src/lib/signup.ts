import { siteConfig } from '../site.config';

export const signupConfigured = siteConfig.signup.buttondownUser.trim() !== '';
/** Production builds leave the section out until a Buttondown username is set; dev shows it with a note. */
export const signupVisible = signupConfigured || import.meta.env.DEV;
export const signupAction = signupConfigured
  ? `https://buttondown.com/api/emails/embed-subscribe/${encodeURIComponent(siteConfig.signup.buttondownUser.trim())}`
  : '';
