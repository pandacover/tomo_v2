import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "tomo | your context, carried forward",
    template: "%s | tomo",
  },
  description: "A private personal assistant for keeping track of the details you choose to share.",
  openGraph: {
    title: "tomo | your context, carried forward",
    description: "A private personal assistant for keeping track of the details you choose to share.",
    type: "website",
  },
  twitter: {
    card: "summary",
    title: "tomo | your context, carried forward",
    description: "A private personal assistant for keeping track of the details you choose to share.",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      >
        <body className="min-h-full flex flex-col">
          <main className="flex-1">{children}</main>
        </body>
    </html>
  );
}
