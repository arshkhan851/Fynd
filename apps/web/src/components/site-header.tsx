import Link from "next/link";

export function SiteHeader() {
  return (
    <header className="site-header">
      <div className="site-header__inner">
        <Link className="brand" href="/" aria-label="Fynd home">
          <span className="brand__mark" aria-hidden="true" />
          <span>fynd</span>
        </Link>
        <nav aria-label="Main navigation" className="site-nav">
          <Link href="/#competitions">Competitions</Link>
          <Link href="/saved-opportunities">Saved Opportunities</Link>
          <Link href="/submit">Submit an event</Link>
        </nav>
      </div>
    </header>
  );
}
