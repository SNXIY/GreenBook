import type { SVGProps } from "react";

export type IconProps = SVGProps<SVGSVGElement>;

const createIcon = (path: JSX.Element) => (props: IconProps) => (
  <svg
    width={props.width ?? 24}
    height={props.height ?? 24}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.6}
    strokeLinecap="round"
    strokeLinejoin="round"
    {...props}
  >
    {path}
  </svg>
);

export const HomeIcon = createIcon(
  <>
    <path d="M3.6 10.3 11.1 3.8a1.3 1.3 0 0 1 1.7 0l7.6 6.5" />
    <path d="M5.5 9.5V19a1 1 0 0 0 1 1h10.9a1 1 0 0 0 1-1v-9.4" />
    <path d="M9.5 20V13h5v7" />
  </>
);

export const CreateIcon = createIcon(
  <>
    <path d="M5.5 19v-5.1a2 2 0 0 1 2-2h3.3" />
    <path d="M5.5 19h4.6" />
    <path d="M13.6 5.4c.8-.8 2-.8 2.8 0l1.7 1.7c.8.8.8 2 0 2.8L12 15l-3.4.9.9-3.4z" />
  </>
);

export const TaskIcon = createIcon(
  <>
    <rect x="5" y="4.5" width="14" height="16" rx="2" />
    <path d="M9 4.5v-1h6v1M8.5 9h7M8.5 13h4.5M8.5 17h6.5" />
  </>
);

export const ProfileIcon = createIcon(
  <>
    <circle cx="12" cy="8.3" r="3.2" />
    <path d="M6.2 19.6a6.6 6.6 0 0 1 11.6 0" />
  </>
);

export const SparkIcon = createIcon(
  <>
    <path d="m12 2.8 1.5 4.6h4.9l-3.9 2.9 1.4 4.7L12 12.7 8.1 15l1.4-4.7-3.9-2.9h4.9z" />
  </>
);

export const LeafIcon = createIcon(
  <>
    <path d="M19.5 4.5C12.6 4.6 6.8 7.4 5.2 12.1c-1.1 3.2.7 6.4 3.9 6.8 4.8.6 8.9-4.8 10.4-14.4Z" />
    <path d="M5 20c2.2-4.4 5.8-7.8 10.8-10.2" />
  </>
);

export const ArrowRightIcon = createIcon(
  <>
    <path d="m10 7 5 5-5 5" />
    <path d="M5 12h10" />
  </>
);

export const HeartIcon = createIcon(
  <>
    <path d="M12 19c-4.8-2.7-7.5-5.6-7.5-9a4.5 4.5 0 0 1 8.2-2.4h.6A4.5 4.5 0 0 1 19.5 10c0 3.4-2.7 6.3-7.5 9z" />
  </>
);

export const BookmarkIcon = createIcon(
  <>
    <path d="M7.5 4h9a1 1 0 0 1 1 1v14l-5.5-3.5L6.5 19V5a1 1 0 0 1 1-1z" />
  </>
);

export const BellIcon = createIcon(
  <>
    <path d="M18 9.8a6 6 0 0 0-12 0c0 7-2.3 7.2-2.3 7.2h16.6S18 16.8 18 9.8" />
    <path d="M9.8 20a2.4 2.4 0 0 0 4.4 0" />
  </>
);

export const ShieldIcon = createIcon(
  <>
    <path d="M12 3 19 6v5.1c0 4.5-2.8 7.7-7 9.9-4.2-2.2-7-5.4-7-9.9V6z" />
    <path d="m9.2 12 1.8 1.8 3.9-4" />
  </>
);

export const RefreshIcon = createIcon(
  <>
    <path d="M20 7v5h-5" />
    <path d="M4 17v-5h5" />
    <path d="M6.1 8.2A7 7 0 0 1 18.7 10M5.3 14a7 7 0 0 0 12.6 1.8" />
  </>
);

export const CheckIcon = createIcon(<path d="m5 12.5 4.2 4.2L19 7" />);

export const CloseIcon = createIcon(
  <>
    <path d="m6 6 12 12" />
    <path d="m18 6-12 12" />
  </>
);

export const AlertIcon = createIcon(
  <>
    <path d="M12 4 21 20H3z" />
    <path d="M12 9v4" />
    <path d="M12 17h.01" />
  </>
);

export const ArrowLeftIcon = createIcon(
  <>
    <path d="m11 6-6 6 6 6" />
    <path d="M5 12h14" />
  </>
);

export const AssistantIcon = createIcon(
  <>
    <path d="M12 3.2a7.8 7.8 0 0 0-7.8 7.8v5.6A2.4 2.4 0 0 0 6.6 19h1.1" />
    <path d="M12 3.2a7.8 7.8 0 0 1 7.8 7.8v5.6a2.4 2.4 0 0 1-2.4 2.4h-1.1" />
    <path d="M8.2 11.7h.01M15.8 11.7h.01" />
    <path d="M9.1 15a4.5 4.5 0 0 0 5.8 0" />
  </>
);

export const SendIcon = createIcon(
  <>
    <path d="m4 4 16 8-16 8 2.5-8z" />
    <path d="M6.5 12H20" />
  </>
);

export const ClockIcon = createIcon(
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 2" />
  </>
);

export const SearchIcon = createIcon(
  <>
    <circle cx="10.7" cy="10.7" r="6.4" />
    <path d="m15.4 15.4 4.1 4.1" />
  </>
);
