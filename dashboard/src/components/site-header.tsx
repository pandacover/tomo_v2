import Link from 'next/link';
import { navLinks } from '@/lib/landing-content';

export const SiteHeader = () => {
  return (
    <header className="sticky top-0 z-20 border-b border-tomo-ink/5 bg-tomo-paper/85 backdrop-blur-md">
      <div className="mx-auto flex h-20 w-full max-w-tomo-container items-center justify-between px-tomo-gutter">
        <Link
          href="/"
          className="font-heading text-4xl font-extrabold text-tomo-ink transition-transform active:scale-95"
        >
          tomo
        </Link>
        <nav aria-label="primary" className="hidden items-center gap-8 md:flex">
          {navLinks.map((link) => (
            <a
              key={link.href}
              className="text-sm text-tomo-ink/60 transition-colors hover:text-tomo-ink"
              href={link.href}
            >
              {link.label}
            </a>
          ))}
        </nav>
      </div>
    </header>
  );
};
