"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";

import { CompetitionCategoryOption } from "@/lib/competitions";
import { CURRENT_CONSENT_DECLARATION_TEXT } from "@/lib/submission-consent";
import { createSubmission, getCurrentUser } from "@/lib/submissions";

type Props = { apiBaseUrl: string; categories: CompetitionCategoryOption[]; categoryError?: string };
type Values = {
  title: string; description: string; official_url: string; registration_url: string;
  format: "online" | "offline" | "hybrid"; registration_deadline_on: string;
  event_starts_on: string; event_ends_on: string; organizer_name: string; organizer_email: string;
  organizer_contact_name: string; organizer_contact_email: string; organizer_contact_phone: string;
  location_name: string; location_city: string; location_region: string; location_country_code: string;
  registration_price_status: "free" | "paid" | "unknown"; registration_fee_amount: string;
  registration_fee_currency: string;
};
const initial: Values = {
  title: "", description: "", official_url: "", registration_url: "", format: "online",
  registration_deadline_on: "", event_starts_on: "", event_ends_on: "", organizer_name: "",
  organizer_email: "", organizer_contact_name: "", organizer_contact_email: "",
  organizer_contact_phone: "", location_name: "", location_city: "", location_region: "",
  location_country_code: "", registration_price_status: "unknown", registration_fee_amount: "",
  registration_fee_currency: "",
};

export function SubmissionForm({ apiBaseUrl, categories, categoryError }: Props) {
  const router = useRouter();
  const [userState, setUserState] = useState<"loading" | "signed-out" | "signed-in" | "error">("loading");
  const [values, setValues] = useState(initial);
  const [accepted, setAccepted] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitError, setSubmitError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let active = true;
    getCurrentUser(apiBaseUrl).then((user) => {
      if (active) setUserState(user ? "signed-in" : "signed-out");
    }).catch(() => { if (active) setUserState("error"); });
    return () => { active = false; };
  }, [apiBaseUrl]);

  function setField<K extends keyof Values>(key: K, value: Values[K]) {
    setValues((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: "" }));
  }

  function validate(): boolean {
    const next: Record<string, string> = {};
    if (values.event_ends_on && values.event_starts_on && values.event_ends_on < values.event_starts_on) {
      next.event_ends_on = "End date must be on or after the start date.";
    }
    if (values.registration_price_status === "paid") {
      const amount = Number(values.registration_fee_amount);
      if (!values.registration_fee_amount || !Number.isFinite(amount) || amount <= 0 || amount > 999999999999.99) {
        next.registration_fee_amount = "Enter a fee greater than 0 and within the allowed range.";
      }
      if (!/^[A-Za-z]{3}$/.test(values.registration_fee_currency)) {
        next.registration_fee_currency = "Enter a 3-letter currency code, such as INR.";
      }
    }
    if (!accepted) next.consent = "Accept the permission declaration to submit.";
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitError("");
    if (!validate() || !event.currentTarget.reportValidity()) return;
    setSubmitting(true);
    const optional = (value: string) => value.trim() || undefined;
    try {
      const submission = await createSubmission(apiBaseUrl, {
        title: values.title.trim(), description: values.description.trim(),
        official_url: values.official_url.trim(), registration_url: values.registration_url.trim(),
        format: values.format, registration_deadline_on: values.registration_deadline_on,
        event_starts_on: values.event_starts_on, event_ends_on: values.event_ends_on,
        organizer_name: values.organizer_name.trim(), organizer_email: values.organizer_email.trim(),
        organizer_contact_name: optional(values.organizer_contact_name),
        organizer_contact_email: optional(values.organizer_contact_email),
        organizer_contact_phone: optional(values.organizer_contact_phone),
        location_name: optional(values.location_name), location_city: optional(values.location_city),
        location_region: optional(values.location_region),
        location_country_code: optional(values.location_country_code)?.toUpperCase(),
        registration_price_status: values.registration_price_status,
        ...(values.registration_price_status === "paid" ? {
          registration_fee_amount: values.registration_fee_amount,
          registration_fee_currency: values.registration_fee_currency.toUpperCase(),
        } : {}),
        category_ids: [],
        primary_category_id: null,
        consentAccepted: true,
      });
      if (submission.status !== "pending") throw new Error("The submission response had an unexpected status. Please contact support.");
      router.push(`/submissions?created=${submission.id}`);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "Submission failed. Please try again.");
      setSubmitting(false);
    }
  }

  if (userState === "loading") return <div className="state-panel" role="status">Checking your sign-in…</div>;
  if (userState === "signed-out") return (
    <div className="state-panel">
      <h2>Sign in to submit an event</h2><p>We’ll send a secure sign-in link to your email.</p>
      <Link className="button-link submission-inline-link" href="/login?next=%2Fsubmit">Continue to sign in</Link>
    </div>
  );
  if (userState === "error") return <div className="state-panel state-panel--error" role="alert"><h2>We couldn’t check your session</h2><p>Please try again when the API is available.</p><button className="button-link" onClick={() => location.reload()}>Try again</button></div>;

  const input = (name: keyof Values, label: string, type = "text", required = false, extra?: { maxLength?: number; placeholder?: string }) => (
    <label className="submission-field" key={name}>
      <span>{label}{required && <span aria-hidden="true"> *</span>}</span>
      <input type={type} value={values[name]} required={required} maxLength={extra?.maxLength} placeholder={extra?.placeholder}
        onChange={(event) => setField(name, event.target.value as Values[typeof name])} aria-invalid={Boolean(errors[name])}
        aria-describedby={errors[name] ? `${name}-error` : undefined} />
      {errors[name] && <small id={`${name}-error`} className="field-error">{errors[name]}</small>}
    </label>
  );

  return (
    <form className="submission-form" onSubmit={handleSubmit} noValidate>
      <section className="submission-section" aria-labelledby="event-info"><p className="eyebrow">01 / Event</p><h2 id="event-info">Hackathon information</h2>
        {input("title", "Hackathon title", "text", true, { maxLength: 300 })}
        <label className="submission-field"><span>Description *</span><textarea required rows={6} value={values.description} onChange={(e) => setField("description", e.target.value)} aria-invalid={Boolean(errors.description)} /></label>
        {input("official_url", "Official website", "url", true)}{input("registration_url", "Registration URL", "url", true)}
        <div className="submission-grid submission-grid--3"><label className="submission-field"><span>Format *</span><select required value={values.format} onChange={(e) => setField("format", e.target.value as Values["format"])}><option value="online">Online</option><option value="offline">Offline</option><option value="hybrid">Hybrid</option></select></label>
          {input("registration_deadline_on", "Registration deadline", "date", true)}{input("event_starts_on", "Event starts", "date", true)}{input("event_ends_on", "Event ends", "date", true)}</div>
        {errors.event_ends_on && <small className="field-error">{errors.event_ends_on}</small>}
      </section>

      <section className="submission-section" aria-labelledby="organizer-info"><p className="eyebrow">02 / Organizer</p><h2 id="organizer-info">Who is hosting?</h2>
        <div className="submission-grid">{input("organizer_name", "Organizer name", "text", true)}{input("organizer_email", "Organizer email", "email", true)}</div>
        <p className="submission-hint">Optional contact details for event questions.</p>
        <div className="submission-grid">{input("organizer_contact_name", "Contact name")}{input("organizer_contact_email", "Contact email", "email")}{input("organizer_contact_phone", "Contact phone", "tel", false, { maxLength: 50 })}</div>
      </section>

      <section className="submission-section" aria-labelledby="location-info"><p className="eyebrow">03 / Location</p><h2 id="location-info">Where it happens</h2>
        <div className="submission-grid">{input("location_name", "Venue or location")}{input("location_city", "City")}{input("location_region", "State or region")}{input("location_country_code", "Country code", "text", false, { maxLength: 2, placeholder: "IN" })}</div>
      </section>

      <section className="submission-section" aria-labelledby="pricing-info"><p className="eyebrow">04 / Pricing</p><h2 id="pricing-info">Registration cost</h2>
        <label className="submission-field submission-field--narrow"><span>Registration price *</span><select value={values.registration_price_status} onChange={(e) => setField("registration_price_status", e.target.value as Values["registration_price_status"])}><option value="free">Free</option><option value="paid">Paid</option><option value="unknown">Not sure</option></select></label>
        {values.registration_price_status === "paid" && <div className="submission-grid submission-grid--2">{input("registration_fee_amount", "Fee amount", "number", true)}{input("registration_fee_currency", "Currency code", "text", true, { maxLength: 3, placeholder: "INR" })}</div>}
      </section>

      <section className="submission-section" aria-labelledby="categories-info"><p className="eyebrow">05 / Categories</p><h2 id="categories-info">Help participants find it</h2>
        {categoryError ? <p className="field-error" role="alert">{categoryError}</p> : categories.length === 0 ? <p className="submission-hint">No categories are currently available. You may submit without one.</p> : <>
          <p className="submission-hint">Categories are available, but the current public categories response does not include the IDs required by the submission API. Submit without categories for now.</p>
          <ul className="submission-category-list">{categories.map((category) => <li key={category.slug}>{category.name}</li>)}</ul>
        </>}
      </section>

      <section className="submission-section" aria-labelledby="permission-info"><p className="eyebrow">06 / Permission</p><h2 id="permission-info">Permission to display</h2>
        <div className="consent-copy"><p>{CURRENT_CONSENT_DECLARATION_TEXT}</p></div>
        <label className="consent-check"><input type="checkbox" checked={accepted} onChange={(e) => { setAccepted(e.target.checked); setErrors((current) => ({ ...current, consent: "" })); }} /> <span>I have permission to submit this information and agree to the declaration above. *</span></label>
        {errors.consent && <small className="field-error" role="alert">{errors.consent}</small>}
      </section>

      {submitError && <div className="state-panel state-panel--error" role="alert"><p>{submitError}</p></div>}
      <div className="submission-actions"><p>Submissions are reviewed before they appear publicly.</p><button className="filter-submit" type="submit" disabled={submitting}>{submitting ? "Submitting…" : "Submit hackathon"}</button></div>
    </form>
  );
}
