export type PeerGrant = { communicate: boolean; autoReply: boolean; shareAvailability: boolean; revision: number; expiresAt: string | null };
export type PeerRelationship = { relationshipId: string; peerHandle: string; status: 'pending' | 'active' | 'revoked' | 'blocked'; grant: PeerGrant | null; peerGrant: PeerGrant | null; peerCommunicate: boolean; peerAutoReply: boolean; peerShareAvailability: boolean; relationshipRevision: number; expiresAt: string | null; canAccept: boolean };
export type PeerHistoryEntry = { requestId: string; threadId: string; direction: 'incoming' | 'outgoing'; kind: string; status: string; createdAt: string; responseStatus: string | null };
export type PeerDashboard = { handle: string | null; relationships: PeerRelationship[] };
